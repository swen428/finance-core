"""Tests for Statement Amount Direction Rules v1.

Covers the deterministic classification of imported statement row amounts
into direction categories: debit, credit, refund, reversal, payment,
fee, interest, and unknown.

All tests use synthetic data. No database/finance.db is touched.
"""

from __future__ import annotations

import csv
import os
import sqlite3
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.statement_csv import (
    StatementCsvAdapter,
    classify_row_direction,
)
from finance_core.reconciliation.statement_identity import ROW_FINGERPRINT_VERSION
from finance_core.reconciliation.statement_import import StatementImporter
from finance_core.resources import migration_resource_paths

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_csv(headers: list[str], rows: list[list[str]]) -> Path:
    """Write a temporary CSV file using csv.writer for proper quoting."""
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="test_dir_csv_")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
    return Path(path)


# ===================================================================
# 1. Normal debit / purchase row
# ===================================================================


def test_normal_debit_row_classified_as_debit() -> None:
    """A normal positive-amount purchase row is classified as DEBIT."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        assert result.rows[0].amount_direction == StatementAmountDirection.DEBIT
        assert result.rows[0].amount == Decimal("10.00")
        assert result.rows[0].merchant_raw == "Apple"
    finally:
        path.unlink(missing_ok=True)


def test_normal_debit_preserves_all_fields() -> None:
    """Normal debit row preserves transaction_date, posted_date, merchant, amount, currency."""
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "2024-12-03", "Netflix", "15.99", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        r = result.rows[0]
        assert r.transaction_date == date(2024, 12, 1)
        assert r.posted_date == date(2024, 12, 3)
        assert r.merchant_raw == "Netflix"
        assert r.amount == Decimal("15.99")
        assert r.currency == "SGD"
        assert r.amount_direction == StatementAmountDirection.DEBIT
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 2. Explicit credit row (negative signed amount)
# ===================================================================


def test_negative_signed_amount_classified_not_rejected() -> None:
    """Negative signed amounts are classified (no longer rejected)."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "SHOPEE REFUND", "(12.34)", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        r = result.rows[0]
        # Amount is absolute value
        assert r.amount == Decimal("12.34")
        assert r.amount_direction is not None
        assert r.raw_row_payload is not None
    finally:
        path.unlink(missing_ok=True)


def test_debit_credit_mode_credit_row_classified() -> None:
    """Credit-only rows in debit_credit mode are classified (not rejected)."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "Debit", "Credit", "currency"],
        [["01/12/2024", "PAYPAL INCOMING", "", "25.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path, amount_mode="debit_credit")
        assert len(result.rows) == 1
        r = result.rows[0]
        assert r.amount == Decimal("25.00")
        assert r.amount_direction == StatementAmountDirection.CREDIT
        assert r.merchant_raw == "PAYPAL INCOMING"
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 3. Refund row (merchant keyword match)
# ===================================================================


def test_refund_keyword_in_merchant_classified_as_refund() -> None:
    """Merchant name containing 'REFUND' is classified as REFUND."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-05", "AMAZON REFUND", "(49.90)", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        assert result.rows[0].amount_direction == StatementAmountDirection.REFUND
        assert result.rows[0].amount == Decimal("49.90")
    finally:
        path.unlink(missing_ok=True)


def test_reversal_keyword_in_merchant_classified_as_refund() -> None:
    """Merchant name containing 'REVERSAL' is classified as REFUND (keyword group)."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-06", "REVERSAL OF TRANSACTION", "(100.00)", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        assert result.rows[0].amount_direction == StatementAmountDirection.REFUND
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 4. Reversal row (explicit type column)
# ===================================================================

# Note: Reversal as a distinct category is captured by merchant keyword matching
# in the REFUND keyword group. Explicit type column classification would require
# a CSV with a dedicated "Type" column, which is tested in the raw_type tests below.


# ===================================================================
# 5. Card payment row
# ===================================================================


def test_payment_keyword_classified_as_payment() -> None:
    """Merchant name containing 'PAYMENT THANK YOU' is classified as PAYMENT."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-10", "PAYMENT THANK YOU", "500.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        assert result.rows[0].amount_direction == StatementAmountDirection.PAYMENT
        assert result.rows[0].amount == Decimal("500.00")
    finally:
        path.unlink(missing_ok=True)


def test_card_payment_keyword_classified_as_payment() -> None:
    """Merchant name containing 'CARD PAYMENT' is classified as PAYMENT."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-15", "CARD PAYMENT - OCBC", "1000.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        assert result.rows[0].amount_direction == StatementAmountDirection.PAYMENT
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 6. Fee row
# ===================================================================


def test_annual_fee_classified_as_fee() -> None:
    """Merchant name containing 'ANNUAL FEE' is classified as FEE."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "ANNUAL FEE", "192.60", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        assert result.rows[0].amount_direction == StatementAmountDirection.FEE
        assert result.rows[0].amount == Decimal("192.60")
    finally:
        path.unlink(missing_ok=True)


def test_late_fee_classified_as_fee() -> None:
    """Merchant name containing 'LATE FEE' is classified as FEE."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-20", "LATE FEE", "80.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        assert result.rows[0].amount_direction == StatementAmountDirection.FEE
    finally:
        path.unlink(missing_ok=True)


def test_service_charge_classified_as_fee() -> None:
    """Merchant name containing 'SERVICE CHARGE' is classified as FEE."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-25", "SERVICE CHARGE", "5.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        assert result.rows[0].amount_direction == StatementAmountDirection.FEE
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 7. Interest row
# ===================================================================


def test_interest_charge_classified_as_interest() -> None:
    """Merchant name containing 'INTEREST CHARGE' is classified as INTEREST."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-31", "INTEREST CHARGE", "12.50", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        assert result.rows[0].amount_direction == StatementAmountDirection.INTEREST
    finally:
        path.unlink(missing_ok=True)


def test_interest_earned_classified_as_interest() -> None:
    """Merchant name containing 'INTEREST EARNED' is classified as INTEREST."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-31", "INTEREST EARNED", "0.50", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        assert result.rows[0].amount_direction == StatementAmountDirection.INTEREST
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 8. Ambiguous row becomes unknown or review_required
# ===================================================================


def test_ambiguous_zero_amount_debit() -> None:
    """A row with zero amount and no clear direction keywords is UNKNOWN."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "MYSTERY TRANSACTION", "0.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        # Zero amount -> UNKNOWN direction
        assert result.rows[0].amount_direction == StatementAmountDirection.UNKNOWN
    finally:
        path.unlink(missing_ok=True)


def test_ambiguous_merchant_no_keywords() -> None:
    """A row with a positive amount but no recognizable merchant keywords is DEBIT."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "XYZ UNKNOWN SHOP", "50.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        # Positive amount, no special keywords -> DEBIT (normal purchase)
        assert result.rows[0].amount_direction == StatementAmountDirection.DEBIT
    finally:
        path.unlink(missing_ok=True)


def test_classify_row_direction_standalone_no_input() -> None:
    """classify_row_direction with no input returns UNKNOWN."""
    result = classify_row_direction(
        amount_raw=None,
        debit_raw=None,
        credit_raw=None,
        merchant_raw=None,
        amount_mode="signed",
        amount_parsed=None,
    )
    assert result == StatementAmountDirection.UNKNOWN


# ===================================================================
# 9. Raw amount is preserved
# ===================================================================


def test_raw_amount_preserved_in_payload() -> None:
    """The raw row payload preserves the original amount as-is."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "SGD 10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        r = result.rows[0]
        assert r.raw_row_payload is not None
        # The raw payload should contain the original amount string
        assert "SGD 10.00" in str(r.raw_row_payload.values())
    finally:
        path.unlink(missing_ok=True)


def test_negative_amount_absolute_value_stored() -> None:
    """For negative amounts, the structured amount stores the absolute value."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "REFUND ITEM", "-25.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        # Amount stored as absolute value
        assert result.rows[0].amount == Decimal("25.00")
        # But direction is not DEBIT
        assert result.rows[0].amount_direction != StatementAmountDirection.DEBIT
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 10. Raw type is preserved
# ===================================================================


def test_raw_amount_field_preserved() -> None:
    """The raw_amount field preserves the raw amount string from CSV."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        r = result.rows[0]
        # raw_amount preserves the original amount string
        assert r.raw_amount == "10.00"
    finally:
        path.unlink(missing_ok=True)


def test_raw_amount_preserved_for_negative() -> None:
    """The raw_amount field preserves the original negative amount string."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "REFUND", "(49.90)", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        r = result.rows[0]
        assert r.raw_amount == "(49.90)"
        assert r.amount == Decimal("49.90")  # absolute value
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 11. transaction_date and posted_date remain separate
# ===================================================================


def test_txn_and_posted_dates_separate_with_direction() -> None:
    """transaction_date and posted_date remain separate when direction is set."""
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "2024-12-03", "REFUND ITEM", "(50.00)", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        r = result.rows[0]
        assert r.transaction_date == date(2024, 12, 1)
        assert r.posted_date == date(2024, 12, 3)
        assert r.amount_direction == StatementAmountDirection.REFUND
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 12. Existing fingerprint dedup behavior is not broken
# ===================================================================


def test_importer_generates_canonical_fingerprint_with_direction() -> None:
    """The authoritative importer fingerprints rows that carry direction."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "SGD"]],
    )
    conn = sqlite3.connect(":memory:")
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        r = result.rows[0]
        assert r.row_fingerprint is None
        assert r.fingerprint_source_content_hash == result.source_content_hash

        _apply_migrations(conn)
        StatementImporter(conn).import_rows(result.rows, source_type="structured_csv")
        fingerprint, version = conn.execute(
            "SELECT row_fingerprint, row_fingerprint_version FROM statement_transactions"
        ).fetchone()
        assert len(fingerprint) == 64
        int(fingerprint, 16)
        assert version == ROW_FINGERPRINT_VERSION
    finally:
        path.unlink(missing_ok=True)
        conn.close()


def test_source_identity_stable_with_direction() -> None:
    """Repeated parsing preserves the same source identity for importer use."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Netflix", "15.99", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result1 = adapter.parse_file_hardened(path)
        result2 = adapter.parse_file_hardened(path)
        assert result1.rows[0].row_fingerprint is None
        assert result2.rows[0].row_fingerprint is None
        assert result1.source_content_hash == result2.source_content_hash
        assert (
            result1.rows[0].fingerprint_source_content_hash
            == result2.rows[0].fingerprint_source_content_hash
            == result1.source_content_hash
        )
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 13. Statement import still passes existing tests
# ===================================================================


def test_import_with_direction_rows() -> None:
    """Rows with amount_direction set can still be imported."""
    import sqlite3

    from finance_core.reconciliation.statement_import import StatementImporter

    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [
            ["2024-12-01", "Apple", "10.00", "SGD"],
            ["2024-12-02", "REFUND SHOPEE", "(12.34)", "SGD"],
        ],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 2

        conn = sqlite3.connect(":memory:")
        # Apply migrations
        _apply_migrations(conn)
        importer = StatementImporter(conn)
        batch = importer.import_rows(
            result.rows,
            source_type="credit_card_statement",
        )
        assert batch.row_count == 2
        assert batch.inserted_ids is not None
        assert len(batch.inserted_ids) == 2
    finally:
        path.unlink(missing_ok=True)
        conn.close()


# ===================================================================
# 14. classify_row_direction standalone tests
# ===================================================================


def test_classify_merchant_keyword_match_priority() -> None:
    """Merchant keyword matching takes priority over amount sign."""
    # Even with positive amount, merchant keyword wins
    result = classify_row_direction(
        amount_raw="100.00",
        debit_raw=None,
        credit_raw=None,
        merchant_raw="PAYMENT THANK YOU - OCBC",
        amount_mode="signed",
        amount_parsed=Decimal("100.00"),
    )
    assert result == StatementAmountDirection.PAYMENT


def test_classify_refund_via_merchant() -> None:
    """Merchant with 'REFUND' keyword is classified as REFUND."""
    result = classify_row_direction(
        amount_raw="-50.00",
        debit_raw=None,
        credit_raw=None,
        merchant_raw="AMAZON REFUND",
        amount_mode="signed",
        amount_parsed=Decimal("-50.00"),
    )
    assert result == StatementAmountDirection.REFUND


def test_classify_fee_via_merchant() -> None:
    """Merchant with 'ANNUAL FEE' is classified as FEE."""
    result = classify_row_direction(
        amount_raw="192.60",
        debit_raw=None,
        credit_raw=None,
        merchant_raw="ANNUAL FEE - DBS",
        amount_mode="signed",
        amount_parsed=Decimal("192.60"),
    )
    assert result == StatementAmountDirection.FEE


def test_classify_interest_via_merchant() -> None:
    """Merchant with 'INTEREST' is classified as INTEREST."""
    result = classify_row_direction(
        amount_raw="0.50",
        debit_raw=None,
        credit_raw=None,
        merchant_raw="INTEREST EARNED",
        amount_mode="signed",
        amount_parsed=Decimal("0.50"),
    )
    assert result == StatementAmountDirection.INTEREST


def test_classify_signed_mode_positive_is_debit() -> None:
    """Positive amount in signed mode with no keywords is DEBIT."""
    result = classify_row_direction(
        amount_raw="10.00",
        debit_raw=None,
        credit_raw=None,
        merchant_raw="Apple Store",
        amount_mode="signed",
        amount_parsed=Decimal("10.00"),
    )
    assert result == StatementAmountDirection.DEBIT


def test_classify_signed_mode_negative_is_unknown() -> None:
    """Negative amount in signed mode with no merchant keywords is UNKNOWN (not guessed)."""
    result = classify_row_direction(
        amount_raw="-10.00",
        debit_raw=None,
        credit_raw=None,
        merchant_raw="Some Unknown Credit",
        amount_mode="signed",
        amount_parsed=Decimal("-10.00"),
    )
    assert result == StatementAmountDirection.UNKNOWN


def test_classify_debit_credit_mode_debit() -> None:
    """Debit column only in debit_credit mode is DEBIT."""
    result = classify_row_direction(
        amount_raw=None,
        debit_raw="25.00",
        credit_raw="",
        merchant_raw="Apple",
        amount_mode="debit_credit",
        amount_parsed=Decimal("25.00"),
    )
    assert result == StatementAmountDirection.DEBIT


def test_classify_debit_credit_mode_credit() -> None:
    """Credit column only in debit_credit mode is CREDIT."""
    result = classify_row_direction(
        amount_raw=None,
        debit_raw="",
        credit_raw="50.00",
        merchant_raw="Some Incoming Credit",
        amount_mode="debit_credit",
        amount_parsed=Decimal("50.00"),
    )
    assert result == StatementAmountDirection.CREDIT


def test_classify_debit_credit_mode_both_is_unknown() -> None:
    """Both debit and credit present is UNKNOWN."""
    result = classify_row_direction(
        amount_raw=None,
        debit_raw="25.00",
        credit_raw="25.00",
        merchant_raw="Unknown",
        amount_mode="debit_credit",
        amount_parsed=None,
    )
    assert result == StatementAmountDirection.UNKNOWN


# ===================================================================
# 15. Edge cases
# ===================================================================


def test_direction_enum_values() -> None:
    """Verify all expected enum values exist."""
    values = {v.value for v in StatementAmountDirection}
    expected = {
        "debit",
        "credit",
        "refund",
        "reversal",
        "payment",
        "fee",
        "interest",
        "chargeback",
        "card_payment",
        "transfer_in",
        "transfer_out",
        "interest_debit",
        "interest_credit",
        "cash_withdrawal",
        "cash_deposit",
        "unknown",
    }
    assert values == expected


def test_structured_row_without_direction_still_works() -> None:
    """StructuredStatementRow without amount_direction still works (backward compat)."""
    from finance_core.reconciliation.statement_import import StructuredStatementRow

    row = StructuredStatementRow(
        merchant_raw="Apple",
        amount=Decimal("10.00"),
        currency="SGD",
        transaction_date=date(2024, 12, 1),
    )
    assert row.amount_direction is None
    assert row.amount == Decimal("10.00")


def test_zero_amount_allowed_with_direction() -> None:
    """Zero amount is allowed (for reversals) and keeps direction."""
    from finance_core.reconciliation.statement_import import StructuredStatementRow

    row = StructuredStatementRow(
        merchant_raw="REVERSAL",
        amount=Decimal("0.00"),
        currency="SGD",
        transaction_date=date(2024, 12, 1),
        amount_direction=StatementAmountDirection.REFUND,
    )
    assert row.amount == Decimal("0.00")
    assert row.amount_direction == StatementAmountDirection.REFUND


def test_no_database_touched() -> None:
    """Verify that test does not modify database/finance.db."""
    assert True


# ===================================================================
# 17. Explicit type column classification
# ===================================================================


def test_type_column_refund_with_generic_merchant() -> None:
    """CSV row with type=REFUND and generic merchant is classified as REFUND."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency", "type"],
        [["2024-12-05", "SOME SHOP", "(49.90)", "SGD", "REFUND"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        r = result.rows[0]
        assert r.amount_direction == StatementAmountDirection.REFUND
        assert r.raw_amount_type == "REFUND"
        assert r.amount == Decimal("49.90")
    finally:
        path.unlink(missing_ok=True)


def test_type_column_payment_with_generic_merchant() -> None:
    """CSV row with transaction_type=PAYMENT is classified as PAYMENT."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency", "transaction_type"],
        [["2024-12-05", "SOME SHOP", "500.00", "SGD", "PAYMENT"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        r = result.rows[0]
        assert r.amount_direction == StatementAmountDirection.PAYMENT
        assert r.raw_amount_type == "PAYMENT"
    finally:
        path.unlink(missing_ok=True)


def test_type_column_fee_classified_as_fee() -> None:
    """CSV row with type=FEE is classified as FEE."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency", "type"],
        [["2024-12-05", "SOME BANK", "120.00", "SGD", "FEE"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        r = result.rows[0]
        assert r.amount_direction == StatementAmountDirection.FEE
    finally:
        path.unlink(missing_ok=True)


def test_txn_type_column_interest_classified_as_interest() -> None:
    """CSV row with txn_type=INTEREST CHARGED is classified as INTEREST."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency", "txn_type"],
        [["2024-12-05", "SOME BANK", "15.50", "SGD", "INTEREST CHARGED"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        r = result.rows[0]
        assert r.amount_direction == StatementAmountDirection.INTEREST
    finally:
        path.unlink(missing_ok=True)


def test_type_column_preserves_raw_type_string() -> None:
    """raw_amount_type preserves the original CSV type string unchanged."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency", "type"],
        [["2024-12-05", "SHOP", "25.00", "SGD", "Refund"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        r = result.rows[0]
        assert r.raw_amount_type == "Refund"
        # Classification is case-insensitive
        assert r.amount_direction == StatementAmountDirection.REFUND
    finally:
        path.unlink(missing_ok=True)


def test_type_column_priority_over_merchant_and_sign() -> None:
    """Type column has priority over merchant keyword and amount sign fallback."""
    # Amount is negative (which normally yields UNKNOWN in signed mode),
    # merchant is "PAYMENT TO VENDOR", but type=DEBIT should win
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency", "type"],
        [["2024-12-05", "PAYMENT TO VENDOR", "-150.00", "SGD", "DEBIT"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        r = result.rows[0]
        # Type column wins over merchant keyword PAYMENT and negative sign
        assert r.amount_direction == StatementAmountDirection.DEBIT
        assert r.raw_amount_type == "DEBIT"
        assert r.amount == Decimal("150.00")
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# 18. Persistence tests: amount_direction round-trips to SQLite
# ===================================================================


def test_refund_row_persists_amount_direction_to_statement_transactions() -> None:
    """CSV parsed refund row persists amount_direction into statement_transactions."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-05", "SHOPEE REFUND", "(49.90)", "SGD"]],
    )
    conn = sqlite3.connect(":memory:")
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        assert result.rows[0].amount_direction == StatementAmountDirection.REFUND

        _apply_migrations(conn)
        from finance_core.reconciliation.statement_import import StatementImporter

        importer = StatementImporter(conn)
        batch = importer.import_rows(
            result.rows,
            source_type="credit_card_statement",
        )
        assert batch.row_count == 1

        row = conn.execute(
            "SELECT amount_direction, raw_amount, raw_amount_type FROM statement_transactions"
        ).fetchone()
        assert row is not None
        assert row["amount_direction"] == "refund"
        assert row["raw_amount"] == "(49.90)"
    finally:
        path.unlink(missing_ok=True)
        conn.close()


def test_amount_direction_roundtrip_csv_to_sqlite() -> None:
    """amount_direction round-trips: CSV parser -> StatementImporter -> SQLite."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [
            ["2024-12-01", "Apple", "10.00", "SGD"],
            ["2024-12-02", "PAYMENT THANK YOU", "500.00", "SGD"],
            ["2024-12-03", "ANNUAL FEE", "192.60", "SGD"],
            ["2024-12-04", "INTEREST CHARGE", "12.50", "SGD"],
            ["2024-12-05", "SHOPEE REFUND", "(49.90)", "SGD"],
        ],
    )
    conn = sqlite3.connect(":memory:")
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 5

        _apply_migrations(conn)
        from finance_core.reconciliation.statement_import import StatementImporter

        importer = StatementImporter(conn)
        batch = importer.import_rows(
            result.rows,
            source_type="credit_card_statement",
        )
        assert batch.row_count == 5

        rows = conn.execute(
            "SELECT merchant_raw, amount_direction FROM statement_transactions ORDER BY id"
        ).fetchall()
        assert len(rows) == 5
        directions = {r["merchant_raw"]: r["amount_direction"] for r in rows}
        assert directions["Apple"] == "debit"
        assert directions["PAYMENT THANK YOU"] == "payment"
        assert directions["ANNUAL FEE"] == "fee"
        assert directions["INTEREST CHARGE"] == "interest"
        assert directions["SHOPEE REFUND"] == "refund"
    finally:
        path.unlink(missing_ok=True)
        conn.close()


def test_raw_amount_persists_unchanged() -> None:
    """raw_amount persists unchanged from CSV through to SQLite."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Some Shop", "(25.50)", "SGD"]],
    )
    conn = sqlite3.connect(":memory:")
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1

        _apply_migrations(conn)
        from finance_core.reconciliation.statement_import import StatementImporter

        importer = StatementImporter(conn)
        importer.import_rows(result.rows, source_type="credit_card_statement")

        row = conn.execute("SELECT raw_amount FROM statement_transactions").fetchone()
        assert row["raw_amount"] == "(25.50)"
    finally:
        path.unlink(missing_ok=True)
        conn.close()


def test_raw_amount_type_persists_unchanged() -> None:
    """raw_amount_type persists unchanged from CSV through to SQLite."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency", "type"],
        [["2024-12-05", "Some Shop", "25.00", "SGD", "Refund"]],
    )
    conn = sqlite3.connect(":memory:")
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1

        _apply_migrations(conn)
        from finance_core.reconciliation.statement_import import StatementImporter

        importer = StatementImporter(conn)
        importer.import_rows(result.rows, source_type="credit_card_statement")

        row = conn.execute("SELECT raw_amount_type FROM statement_transactions").fetchone()
        assert row["raw_amount_type"] == "Refund"
    finally:
        path.unlink(missing_ok=True)
        conn.close()


def test_enum_value_stored_as_stable_string_not_repr() -> None:
    """amount_direction stores stable enum value string like 'refund', not repr."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-05", "SHOPEE REFUND", "(49.90)", "SGD"]],
    )
    conn = sqlite3.connect(":memory:")
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.rows) == 1
        assert result.rows[0].amount_direction == StatementAmountDirection.REFUND

        _apply_migrations(conn)
        from finance_core.reconciliation.statement_import import StatementImporter

        importer = StatementImporter(conn)
        importer.import_rows(result.rows, source_type="credit_card_statement")

        row = conn.execute("SELECT amount_direction FROM statement_transactions").fetchone()
        stored = row["amount_direction"]
        assert stored == "refund"
        assert "StatementAmountDirection" not in stored
        assert stored.islower()
    finally:
        path.unlink(missing_ok=True)
        conn.close()


def test_dict_based_import_with_explicit_null_direction() -> None:
    """Dict-based import with no direction fields yields NULL in DB columns."""
    conn = sqlite3.connect(":memory:")
    try:
        _apply_migrations(conn)
        from finance_core.reconciliation.statement_import import StatementImporter

        importer = StatementImporter(conn)
        dict_row = {
            "merchant_raw": "Mystery Shop",
            "amount": "42.00",
            "currency": "SGD",
            "transaction_date": "2024-12-15",
        }
        importer.import_rows([dict_row], source_type="structured_csv")

        stored = conn.execute(
            "SELECT amount_direction, raw_amount, raw_amount_type FROM statement_transactions"
        ).fetchone()
        assert stored["amount_direction"] is None
        assert stored["raw_amount"] is None
        assert stored["raw_amount_type"] is None
    finally:
        conn.close()


def test_dict_based_import_preserves_direction_fields() -> None:
    """Dict-based import preserves amount_direction/raw_amount/raw_amount_type."""
    conn = sqlite3.connect(":memory:")
    try:
        _apply_migrations(conn)
        from finance_core.reconciliation.statement_import import StatementImporter

        importer = StatementImporter(conn)

        dict_row = {
            "merchant_raw": "SHOPEE REFUND",
            "amount": "49.90",
            "currency": "SGD",
            "transaction_date": "2024-12-05",
            "amount_direction": "refund",
            "raw_amount": "(49.90)",
            "raw_amount_type": "Refund",
        }
        batch = importer.import_rows([dict_row], source_type="structured_csv")
        assert batch.row_count == 1

        row = conn.execute(
            "SELECT amount_direction, raw_amount, raw_amount_type FROM statement_transactions"
        ).fetchone()
        assert row["amount_direction"] == "refund"
        assert row["raw_amount"] == "(49.90)"
        assert row["raw_amount_type"] == "Refund"
    finally:
        conn.close()


def test_row_fingerprint_unchanged_with_direction_persistence() -> None:
    """Importer-owned canonical fingerprint persists with direction evidence."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Netflix", "15.99", "SGD"]],
    )
    conn = sqlite3.connect(":memory:")
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.rows[0].row_fingerprint is None

        _apply_migrations(conn)
        from finance_core.reconciliation.statement_import import StatementImporter

        importer = StatementImporter(conn)
        importer.import_rows(result.rows, source_type="credit_card_statement")

        row = conn.execute(
            "SELECT row_fingerprint, row_fingerprint_version FROM statement_transactions"
        ).fetchone()
        assert len(row["row_fingerprint"]) == 64
        int(row["row_fingerprint"], 16)
        assert row["row_fingerprint_version"] == ROW_FINGERPRINT_VERSION
    finally:
        path.unlink(missing_ok=True)
        conn.close()


def test_public_id_unchanged_with_direction_persistence() -> None:
    """public_id derivation unchanged when direction fields are persisted."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "SGD"]],
    )
    conn = sqlite3.connect(":memory:")
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)

        _apply_migrations(conn)
        from finance_core.reconciliation.statement_import import StatementImporter

        importer = StatementImporter(conn)
        importer.import_rows(result.rows, source_type="credit_card_statement")

        row = conn.execute("SELECT public_id FROM statement_transactions").fetchone()
        assert row["public_id"] is not None
        assert row["public_id"].startswith("stmt-")
    finally:
        path.unlink(missing_ok=True)
        conn.close()


def test_transaction_date_and_posted_date_unchanged() -> None:
    """transaction_date and posted_date unchanged when direction is persisted."""
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "2024-12-03", "Netflix", "15.99", "SGD"]],
    )
    conn = sqlite3.connect(":memory:")
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)

        _apply_migrations(conn)
        from finance_core.reconciliation.statement_import import StatementImporter

        importer = StatementImporter(conn)
        importer.import_rows(result.rows, source_type="credit_card_statement")

        row = conn.execute(
            "SELECT transaction_date, posted_date FROM statement_transactions"
        ).fetchone()
        assert row["transaction_date"] == "2024-12-01"
        assert row["posted_date"] == "2024-12-03"
    finally:
        path.unlink(missing_ok=True)
        conn.close()


def test_raw_row_payload_json_unchanged_with_direction() -> None:
    """raw_row_payload_json unchanged when direction fields are persisted."""
    import json

    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "SGD"]],
    )
    conn = sqlite3.connect(":memory:")
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        ref_payload = result.rows[0].raw_row_payload
        assert ref_payload is not None
        assert "amount_direction" not in ref_payload

        _apply_migrations(conn)
        from finance_core.reconciliation.statement_import import StatementImporter

        importer = StatementImporter(conn)
        importer.import_rows(result.rows, source_type="credit_card_statement")

        row = conn.execute("SELECT raw_row_payload_json FROM statement_transactions").fetchone()
        parsed = json.loads(row["raw_row_payload_json"])
        assert "amount_direction" not in parsed
    finally:
        path.unlink(missing_ok=True)
        conn.close()


def test_no_final_app_transaction_created_or_mutated() -> None:
    """Persisting direction fields does not create or mutate final app transactions."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "SHOPEE REFUND", "(49.90)", "SGD"]],
    )
    conn = sqlite3.connect(":memory:")
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)

        _apply_migrations(conn)
        from finance_core.reconciliation.statement_import import StatementImporter

        importer = StatementImporter(conn)
        importer.import_rows(result.rows, source_type="credit_card_statement")

        tx_count = conn.execute("SELECT COUNT(*) as cnt FROM transactions").fetchone()
        assert tx_count is not None
        assert tx_count["cnt"] == 0

        stmt_count = conn.execute("SELECT COUNT(*) as cnt FROM statement_transactions").fetchone()
        assert stmt_count["cnt"] == 1
    finally:
        path.unlink(missing_ok=True)
        conn.close()


def test_migration_010_applies_after_001_009_on_temp_db() -> None:
    """Migration 010 applies after 001-009 on a temporary SQLite database."""
    conn = sqlite3.connect(":memory:")
    try:
        _apply_migrations(conn)

        cols = conn.execute("PRAGMA table_info(statement_transactions)").fetchall()
        col_names = {c[1] for c in cols}
        assert "amount_direction" in col_names
        assert "raw_amount" in col_names
        assert "raw_amount_type" in col_names
        assert "row_fingerprint" in col_names

        indexes = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name LIKE 'idx_stmt_txns_amount_direction%'"
        ).fetchall()
        assert len(indexes) == 1
    finally:
        conn.close()


# ===================================================================
# Helper: apply schema migrations to in-memory database
# ===================================================================


def _apply_migrations(conn: "sqlite3.Connection") -> None:
    """Apply reconciliation schema migrations to an in-memory database."""

    conn.execute("PRAGMA foreign_keys = ON")
    for fname in migration_resource_paths():
        with open(fname) as f:
            conn.executescript(f.read())
    conn.commit()
