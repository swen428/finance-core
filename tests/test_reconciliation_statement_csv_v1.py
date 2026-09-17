"""Tests for Statement CSV Adapter v1.

Covers:
  1. parse minimal CSV with required columns
  2. parse CSV with optional posted_date
  3. parse CSV with statement_row_reference
  4. parse CSV with raw_row_payload
  5. parse CSV with all columns present
  6. reject CSV missing required columns
  7. reject malformed amount
  8. reject malformed date
  9. skipped empty merchant / amount rows
 10. parse_text method
 11. empty file / no header
 12. CsvImportResult structure
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance_core.reconciliation.statement_csv import (
    CsvImportResult,
    StatementCsvAdapter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_csv(headers: list[str], rows: list[list[str]]) -> Path:
    """Write a temporary CSV file using csv.writer for proper quoting."""
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="test_recon_csv_")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
    return Path(path)


def _csv_text(content: str) -> str:
    """Return clean CSV text with consistent line endings."""
    return content.strip() + "\n"


# ---------------------------------------------------------------------------
# 1. parse minimal CSV
# ---------------------------------------------------------------------------


def test_parse_minimal_csv() -> None:
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [
            ["2024-12-01", "Apple", "10.00", "SGD"],
            ["2024-12-02", "Netflix", "15.99", "SGD"],
        ],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file(path)
        assert result.success
        assert len(result.rows) == 2
        assert result.rows[0].merchant_raw == "Apple"
        assert result.rows[0].amount == Decimal("10.00")
        assert result.rows[0].currency == "SGD"
        assert result.rows[0].transaction_date == date(2024, 12, 1)
        assert result.rows[0].posted_date is None
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2. parse CSV with optional posted_date
# ---------------------------------------------------------------------------


def test_csv_with_posted_date() -> None:
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [
            ["2024-12-01", "2024-12-03", "Apple", "10.00", "SGD"],
            ["2024-12-02", "", "Netflix", "15.99", "SGD"],
        ],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file(path)
        assert result.success
        assert result.rows[0].posted_date == date(2024, 12, 3)
        assert result.rows[1].posted_date is None
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. parse CSV with statement_row_reference
# ---------------------------------------------------------------------------


def test_csv_with_statement_row_reference() -> None:
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency", "statement_row_reference"],
        [
            ["2024-12-01", "Apple", "10.00", "SGD", "csv-line-1"],
            ["2024-12-02", "Netflix", "15.99", "SGD", "csv-line-2"],
        ],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file(path)
        assert result.success
        assert result.rows[0].statement_row_reference == "csv-line-1"
        assert result.rows[1].statement_row_reference == "csv-line-2"
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. parse CSV with raw_row_payload
# ---------------------------------------------------------------------------


def test_csv_with_raw_row_payload() -> None:
    payload = json.dumps({"csv_line": 1, "bank_ref": "ABC123"})
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency", "raw_row_payload"],
        [
            ["2024-12-01", "Apple", "10.00", "SGD", payload],
        ],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file(path)
        assert result.success
        assert result.rows[0].raw_row_payload == {"csv_line": 1, "bank_ref": "ABC123"}
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 5. parse CSV with all columns present
# ---------------------------------------------------------------------------


def test_csv_with_all_columns() -> None:
    path = _write_csv(
        [
            "transaction_date",
            "posted_date",
            "merchant_raw",
            "merchant_normalized",
            "amount",
            "currency",
            "account_name",
            "account_id",
            "statement_row_reference",
            "raw_row_payload",
        ],
        [
            [
                "2024-12-01",
                "2024-12-03",
                "Apple",
                "apple inc",
                "29.90",
                "SGD",
                "Owner Visa",
                "acc-001",
                "line-1",
                "{}",
            ],
        ],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file(path)
        assert result.success
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.transaction_date == date(2024, 12, 1)
        assert row.posted_date == date(2024, 12, 3)
        assert row.merchant_raw == "Apple"
        assert row.merchant_normalized == "apple inc"
        assert row.amount == Decimal("29.90")
        assert row.currency == "SGD"
        assert row.account_name == "Owner Visa"
        assert row.account_id == "acc-001"
        assert row.statement_row_reference == "line-1"
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 6. reject CSV missing required columns
# ---------------------------------------------------------------------------


def test_reject_missing_required_columns() -> None:
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount"],  # missing currency
        [],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file(path)
        assert not result.success
        assert result.error_count == 1
        assert "currency" in result.errors[0]
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 7. reject malformed amount
# ---------------------------------------------------------------------------


def test_reject_malformed_amount() -> None:
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "not-a-number", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file(path)
        assert result.error_count == 1
        assert len(result.rows) == 0
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 8. reject malformed date
# ---------------------------------------------------------------------------


def test_reject_malformed_date() -> None:
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["01-12-2024", "Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file(path)
        assert result.error_count == 1
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 9. skipped empty merchant / amount rows
# ---------------------------------------------------------------------------


def test_skip_empty_merchant_row() -> None:
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [
            ["2024-12-01", "", "10.00", "SGD"],  # empty merchant -> skipped
            ["2024-12-02", "Netflix", "15.99", "SGD"],
        ],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file(path)
        assert result.success
        assert result.skipped_count == 1
        assert len(result.rows) == 1
        assert result.rows[0].merchant_raw == "Netflix"
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 10. parse_text method
# ---------------------------------------------------------------------------


def test_parse_text() -> None:
    csv_content = "transaction_date,merchant_raw,amount,currency\n2024-12-01,Apple,10.00,SGD\n"
    adapter = StatementCsvAdapter()
    result = adapter.parse_text(csv_content)
    assert result.success
    assert len(result.rows) == 1
    assert result.rows[0].merchant_raw == "Apple"


# ---------------------------------------------------------------------------
# 11. empty file / no header
# ---------------------------------------------------------------------------


def test_empty_file() -> None:
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="test_empty_")
    os.close(fd)  # create empty file
    path = Path(path)
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file(path)
        assert not result.success
        assert "No header" in result.errors[0]
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 12. CsvImportResult structure
# ---------------------------------------------------------------------------


def test_csv_import_result_structure() -> None:
    csv_content = "transaction_date,merchant_raw,amount,currency\n2024-12-01,Apple,10.00,SGD\n"
    adapter = StatementCsvAdapter()
    result = adapter.parse_text(csv_content)
    assert isinstance(result, CsvImportResult)
    assert result.success is True
    assert result.error_count == 0
    assert result.skipped_count == 0
    assert result.errors == []
    assert len(result.rows) > 0


# ---------------------------------------------------------------------------
# 13. auto-assigned statement_row_reference from line number
# ---------------------------------------------------------------------------


def test_csv_auto_assigns_row_reference_from_line_number() -> None:
    """CSV rows without explicit statement_row_reference get auto-assigned
    'csv-line-{N}' based on CSV line number."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [
            ["2024-12-01", "Apple", "10.00", "SGD"],
            ["2024-12-01", "Apple", "10.00", "SGD"],  # same content, different line
        ],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file(path)
        assert result.success
        assert len(result.rows) == 2
        assert result.rows[0].statement_row_reference == "csv-line-2"
        assert result.rows[1].statement_row_reference == "csv-line-3"
        # Auto-assigned refs must differ
        assert result.rows[0].statement_row_reference != result.rows[1].statement_row_reference
    finally:
        path.unlink(missing_ok=True)


def test_csv_preserves_explicit_row_reference() -> None:
    """Explicit statement_row_reference from CSV is preserved, not overwritten."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency", "statement_row_reference"],
        [
            ["2024-12-01", "Apple", "10.00", "SGD", "bank-ref-abc"],
        ],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file(path)
        assert result.success
        assert len(result.rows) == 1
        assert result.rows[0].statement_row_reference == "bank-ref-abc"
    finally:
        path.unlink(missing_ok=True)
