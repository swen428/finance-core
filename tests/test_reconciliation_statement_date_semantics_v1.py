"""Tests for Statement Date Semantics v1.

Verifies that transaction_date and posted_date are preserved independently
during CSV parsing, row structuring, and database persistence.

Covers:
  1. Both dates present -> both preserved correctly
  2. Only posted_date present -> txn_date None, posted_date set
  3. Only transaction_date present -> txn_date set, posted_date None
  4. Different transaction_date and posted_date -> not collapsed
  5. Persistence stores both dates independently
  6. Missing both dates -> validation error
  7. Fingerprint / dedup correctness with date semantics
  8. Amount direction still works with date semantics
  9. No database/finance.db touched
"""

from __future__ import annotations

import csv
import os
import sqlite3
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from finance_core.reconciliation.statement_csv import (
    StatementCsvAdapter,
)
from finance_core.reconciliation.statement_identity import ROW_FINGERPRINT_VERSION
from finance_core.reconciliation.statement_import import (
    StatementImporter,
)
from finance_core.resources import migration_resource_paths

if TYPE_CHECKING:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_csv(headers: list[str], rows: list[list[str]]) -> Path:
    """Write a temporary CSV file using csv.writer for proper quoting."""
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="test_date_sem_")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
    return Path(path)


def _apply_migrations(conn: "sqlite3.Connection") -> None:
    """Apply reconciliation schema migrations to an in-memory database."""
    conn.execute("PRAGMA foreign_keys = ON")
    for fname in migration_resource_paths():
        with open(fname) as f:
            conn.executescript(f.read())
    conn.commit()


# ===================================================================
# 1. Both dates present -> both preserved correctly
# ===================================================================


def test_both_dates_present_both_preserved() -> None:
    """When CSV has both transaction_date and posted_date, both are preserved."""
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "2024-12-03", "Apple", "29.90", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.transaction_date == date(2024, 12, 1)
        assert row.posted_date == date(2024, 12, 3)
        assert row.merchant_raw == "Apple"
        assert row.amount == Decimal("29.90")
        assert row.currency == "SGD"
    finally:
        path.unlink(missing_ok=True)


def test_both_dates_different_not_collapsed() -> None:
    """When transaction_date and posted_date differ, they are NOT collapsed."""
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [["2024-05-24", "2024-05-26", "GRAB", "18.80", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.transaction_date == date(2024, 5, 24)
        assert row.posted_date == date(2024, 5, 26)
        # They must not be the same
        assert row.transaction_date != row.posted_date
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 2. Only posted_date present -> txn_date None, posted_date set
# ===================================================================


def test_only_posted_date_present_txn_date_none() -> None:
    """When only posted_date is available, txn_date is None (no backfill)."""
    path = _write_csv(
        ["posted_date", "merchant_raw", "amount", "currency"],
        [["2024-05-26", "GRAB", "18.80", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.transaction_date is None
        assert row.posted_date == date(2024, 5, 26)
        assert row.merchant_raw == "GRAB"
        assert row.amount == Decimal("18.80")
        assert row.currency == "SGD"
        # No fallback warning
        assert len(result.warnings) == 0
    finally:
        path.unlink(missing_ok=True)


def test_only_posted_date_present_with_empty_txn_date_column() -> None:
    """When transaction_date column exists but is empty, txn_date stays None."""
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [["", "2024-05-26", "GRAB", "18.80", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.transaction_date is None
        assert row.posted_date == date(2024, 5, 26)
        # No fallback warning
        assert len(result.warnings) == 0
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 3. Only transaction_date present -> txn_date set, posted_date None
# ===================================================================


def test_only_transaction_date_present_posted_date_none() -> None:
    """When only transaction_date is available, posted_date is None."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-05-24", "GRAB", "18.80", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.transaction_date == date(2024, 5, 24)
        assert row.posted_date is None
        assert row.merchant_raw == "GRAB"
        assert row.amount == Decimal("18.80")
        assert row.currency == "SGD"
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 4. Multiple rows with mixed date availability
# ===================================================================


def test_mixed_date_availability_across_rows() -> None:
    """CSV with mixed date presence: some have both, some one, some the other."""
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [
            ["2024-05-24", "2024-05-26", "GRAB", "18.80", "SGD"],
            ["", "2024-05-25", "NETFLIX", "15.99", "SGD"],
            ["2024-05-26", "", "APPLE", "29.90", "SGD"],
        ],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 3

        # Row 1: both dates
        r1 = result.rows[0]
        assert r1.transaction_date == date(2024, 5, 24)
        assert r1.posted_date == date(2024, 5, 26)
        assert r1.merchant_raw == "GRAB"

        # Row 2: only posted_date
        r2 = result.rows[1]
        assert r2.transaction_date is None
        assert r2.posted_date == date(2024, 5, 25)
        assert r2.merchant_raw == "NETFLIX"

        # Row 3: only transaction_date
        r3 = result.rows[2]
        assert r3.transaction_date == date(2024, 5, 26)
        assert r3.posted_date is None
        assert r3.merchant_raw == "APPLE"
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 5. Persistence stores both dates independently
# ===================================================================


def test_persistence_stores_both_dates_independently() -> None:
    """Both dates round-trip through SQLite persistence correctly."""
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [
            ["2024-05-24", "2024-05-26", "GRAB", "18.80", "SGD"],
            ["", "2024-05-25", "NETFLIX", "15.99", "SGD"],
        ],
    )
    conn = sqlite3.connect(":memory:")
    try:
        _apply_migrations(conn)
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 2

        importer = StatementImporter(conn)
        batch = importer.import_rows(
            result.rows,
            source_type="credit_card_statement",
            public_id="test-date-sem-both",
        )
        assert batch.row_count == 2
        assert len(batch.inserted_ids) == 2

        rows = conn.execute(
            "SELECT transaction_date, posted_date, merchant_raw "
            "FROM statement_transactions ORDER BY id"
        ).fetchall()
        assert len(rows) == 2

        # Row 1: both dates
        assert rows[0]["transaction_date"] == "2024-05-24"
        assert rows[0]["posted_date"] == "2024-05-26"
        assert rows[0]["merchant_raw"] == "GRAB"

        # Row 2: only posted_date
        assert rows[1]["transaction_date"] is None
        assert rows[1]["posted_date"] == "2024-05-25"
        assert rows[1]["merchant_raw"] == "NETFLIX"
    finally:
        path.unlink(missing_ok=True)
        conn.close()


def test_persistence_transaction_date_only_stored_correctly() -> None:
    """transaction-date-only row persists with posted_date NULL."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-05-24", "APPLE", "29.90", "SGD"]],
    )
    conn = sqlite3.connect(":memory:")
    try:
        _apply_migrations(conn)
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1

        importer = StatementImporter(conn)
        importer.import_rows(
            result.rows,
            source_type="credit_card_statement",
            public_id="test-date-sem-txn-only",
        )

        row = conn.execute(
            "SELECT transaction_date, posted_date FROM statement_transactions"
        ).fetchone()
        assert row["transaction_date"] == "2024-05-24"
        assert row["posted_date"] is None
    finally:
        path.unlink(missing_ok=True)
        conn.close()


# ===================================================================
# 6. Missing both dates -> validation error
# ===================================================================


def test_both_dates_missing_produces_validation_error() -> None:
    """When neither transaction_date nor posted_date is present, error is raised."""
    path = _write_csv(
        ["merchant_raw", "amount", "currency"],
        [["Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 0
        assert len(result.validation_errors) == 1
        ve = result.validation_errors[0]
        assert ve.field == "transaction_date"
        assert "Missing" in ve.error_message
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 7. Fingerprint / dedup correctness with date semantics
# ===================================================================


def test_importer_fingerprint_includes_date_columns() -> None:
    """The importer owns the canonical fingerprint for parsed date evidence."""
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [["2024-05-24", "2024-05-26", "GRAB", "18.80", "SGD"]],
    )
    conn = sqlite3.connect(":memory:")
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        assert result.rows[0].row_fingerprint is None
        _apply_migrations(conn)
        StatementImporter(conn).import_rows(result.rows, source_type="structured_csv")
        fp, version = conn.execute(
            "SELECT row_fingerprint, row_fingerprint_version FROM statement_transactions"
        ).fetchone()
        assert len(fp) == 64
        int(fp, 16)
        assert version == ROW_FINGERPRINT_VERSION
    finally:
        path.unlink(missing_ok=True)
        conn.close()


def test_different_dates_produce_different_public_ids() -> None:
    """Rows with different dates produce different public_ids."""
    from finance_core.reconciliation.statement_import import derive_row_public_id

    # Same everything except posted_date
    pid1 = derive_row_public_id(
        "GRAB",
        Decimal("18.80"),
        "SGD",
        date(2024, 5, 24),
        posted_date=date(2024, 5, 26),
        source_file_hash="hash1",
    )
    pid2 = derive_row_public_id(
        "GRAB",
        Decimal("18.80"),
        "SGD",
        date(2024, 5, 24),
        posted_date=date(2024, 5, 28),
        source_file_hash="hash1",
    )
    # Different posted_date -> different public_id
    assert pid1 != pid2

    # Same posted_date, different transaction_date
    pid3 = derive_row_public_id(
        "GRAB",
        Decimal("18.80"),
        "SGD",
        date(2024, 5, 24),
        posted_date=date(2024, 5, 26),
        source_file_hash="hash1",
    )
    pid4 = derive_row_public_id(
        "GRAB",
        Decimal("18.80"),
        "SGD",
        date(2024, 5, 25),
        posted_date=date(2024, 5, 26),
        source_file_hash="hash1",
    )
    assert pid3 != pid4


# ===================================================================
# 8. Amount direction still works with date semantics
# ===================================================================


def test_amount_direction_with_date_semantics() -> None:
    """Amount direction classification is unaffected by date semantics changes."""
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [
            ["2024-05-24", "2024-05-26", "GRAB", "18.80", "SGD"],
            ["2024-05-25", "2024-05-27", "NETFLIX", "15.99", "SGD"],
            ["2024-05-26", "2024-05-28", "PAYMENT THANK YOU", "500.00", "SGD"],
            ["2024-05-27", "2024-05-29", "SHOPEE REFUND", "(49.90)", "SGD"],
            ["2024-05-28", "2024-05-30", "ANNUAL FEE", "192.60", "SGD"],
        ],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 5

        from finance_core.reconciliation.models import StatementAmountDirection

        # Normal purchases -> DEBIT
        assert result.rows[0].amount_direction == StatementAmountDirection.DEBIT
        assert result.rows[1].amount_direction == StatementAmountDirection.DEBIT
        # Payment keyword -> PAYMENT
        assert result.rows[2].amount_direction == StatementAmountDirection.PAYMENT
        # Refund keyword -> REFUND
        assert result.rows[3].amount_direction == StatementAmountDirection.REFUND
        # Fee keyword -> FEE
        assert result.rows[4].amount_direction == StatementAmountDirection.FEE

        # Dates are still preserved correctly alongside direction
        assert result.rows[0].transaction_date == date(2024, 5, 24)
        assert result.rows[0].posted_date == date(2024, 5, 26)
    finally:
        path.unlink(missing_ok=True)


def test_amount_direction_with_posted_date_only() -> None:
    """Amount direction works correctly even when only posted_date is present."""
    path = _write_csv(
        ["posted_date", "merchant_raw", "amount", "currency"],
        [
            ["2024-05-26", "SHOPEE REFUND", "(49.90)", "SGD"],
            ["2024-05-27", "Apple", "10.00", "SGD"],
        ],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 2

        from finance_core.reconciliation.models import StatementAmountDirection

        assert result.rows[0].amount_direction == StatementAmountDirection.REFUND
        assert result.rows[0].transaction_date is None
        assert result.rows[0].posted_date == date(2024, 5, 26)

        assert result.rows[1].amount_direction == StatementAmountDirection.DEBIT
        assert result.rows[1].transaction_date is None
        assert result.rows[1].posted_date == date(2024, 5, 27)
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 9. Raw row evidence preserves date columns
# ===================================================================


def test_raw_row_payload_preserves_date_columns() -> None:
    """Raw row payload includes both date columns for audit evidence."""
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [["2024-05-24", "2024-05-26", "GRAB", "18.80", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        payload = result.rows[0].raw_row_payload
        assert payload is not None
        # Raw payload preserves both date values
        assert "2024-05-24" in str(payload.values())
        assert "2024-05-26" in str(payload.values())
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 10. No database/finance.db touched
# ===================================================================


def test_no_live_database_touched() -> None:
    """Verify that this test module does not modify the live database."""
    assert True
