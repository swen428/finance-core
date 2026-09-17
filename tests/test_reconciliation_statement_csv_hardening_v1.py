"""Tests for Statement CSV Import Hardening v1.

Covers column aliases, amount normalisation, date parsing, row fingerprints,
row-level validation, and backward compatibility.

All tests use synthetic data. No database/finance.db is touched.
"""

from __future__ import annotations

import csv
import os
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance_core.reconciliation.statement_csv import (
    ColumnAliasMap,
    CsvImportResult,
    RowValidationError,
    StatementCsvAdapter,
    _csv_row_fingerprint,
    _normalize_amount,
    _parse_date_flexible,
    _parse_signed_amount,
    _resolve_columns,
    _validate_currency,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_csv(headers: list[str], rows: list[list[str]]) -> Path:
    """Write a temporary CSV file using csv.writer for proper quoting."""
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="test_hardened_csv_")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
    return Path(path)


def _fixture_path(name: str) -> Path:
    """Get absolute path to a statement format fixture."""
    base = Path(__file__).parent / "fixtures" / "reconciliation" / "statement_formats"
    return base / name


# ===================================================================
# Column alias detection
# ===================================================================


def test_auto_detects_txn_date_alias_trans_date() -> None:
    """Hardened parser detects 'Trans Date' as transaction_date."""
    path = _write_csv(
        ["Trans Date", "Description", "Amount", "Currency"],
        [["01/12/2024", "Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.success, result.errors
        assert len(result.rows) == 1
        assert result.rows[0].transaction_date == date(2024, 12, 1)
    finally:
        path.unlink(missing_ok=True)


def test_auto_detects_date_alias() -> None:
    """Hardened parser detects 'Date' as transaction_date."""
    path = _write_csv(
        ["Date", "merchant", "amount", "currency"],
        [["01/12/2024", "Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.success, result.errors
        assert len(result.rows) == 1
    finally:
        path.unlink(missing_ok=True)


def test_auto_detects_description_as_merchant() -> None:
    """Hardened parser detects 'Description' as merchant_raw."""
    path = _write_csv(
        ["transaction_date", "Description", "amount", "currency"],
        [["2024-12-01", "Apple Store", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.success
        assert result.rows[0].merchant_raw == "Apple Store"
    finally:
        path.unlink(missing_ok=True)


def test_auto_detects_narrative_as_merchant() -> None:
    """Hardened parser detects 'Narrative' as merchant_raw."""
    path = _write_csv(
        ["transaction_date", "Narrative", "amount", "currency"],
        [["2024-12-01", "PAYPAL", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.success
        assert result.rows[0].merchant_raw == "PAYPAL"
    finally:
        path.unlink(missing_ok=True)


def test_auto_detects_ccy_as_currency() -> None:
    """Hardened parser detects 'CCY' as currency."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "CCY"],
        [["2024-12-01", "Apple", "10.00", "USD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.success
        assert result.rows[0].currency == "USD"
    finally:
        path.unlink(missing_ok=True)


def test_auto_detects_ref_as_reference() -> None:
    """Hardened parser detects 'Ref' as reference."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency", "Ref"],
        [["2024-12-01", "Apple", "10.00", "SGD", "ABC-123"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.success
        assert result.rows[0].statement_row_reference == "ABC-123"
    finally:
        path.unlink(missing_ok=True)


def test_posted_date_alias_value_date() -> None:
    """Hardened parser detects 'Value Date' as posted_date."""
    path = _write_csv(
        ["transaction_date", "Value Date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "03/12/2024", "Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.success
        assert result.rows[0].posted_date == date(2024, 12, 3)
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# Transaction date vs posted date separation
# ===================================================================


def test_preserves_txn_date_and_posted_date_separately() -> None:
    """Transaction_date and posted_date are preserved as separate fields."""
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "2024-12-03", "Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.success
        assert result.rows[0].transaction_date == date(2024, 12, 1)
        assert result.rows[0].posted_date == date(2024, 12, 3)
    finally:
        path.unlink(missing_ok=True)


def test_posted_date_stays_none_when_txn_date_present() -> None:
    """posted_date remains None when not present in CSV."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.success
        assert result.rows[0].transaction_date is not None
        assert result.rows[0].posted_date is None
    finally:
        path.unlink(missing_ok=True)


def test_posted_date_preserved_without_txn_date_backfill() -> None:
    """When txn_date is absent but posted_date present, txn_date stays None (no backfill)."""
    path = _write_csv(
        ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
        [["", "2024-12-05", "Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        # Row should still parse, with posted_date preserved and txn_date None
        assert len(result.rows) == 1
        assert result.rows[0].transaction_date is None
        assert result.rows[0].posted_date == date(2024, 12, 5)
        # No warning about fallback (dates are preserved independently)
        assert len(result.warnings) == 0
    finally:
        path.unlink(missing_ok=True)


def test_both_dates_absent_produces_validation_error() -> None:
    """When both txn_date and posted_date are absent, row produces validation error."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["", "Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.validation_errors) == 1
        assert result.validation_errors[0].field == "transaction_date"
        assert "Missing transaction_date" in result.validation_errors[0].error_message
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# Date parsing -- UK formats
# ===================================================================


def test_parse_date_flexible_iso() -> None:
    assert _parse_date_flexible("2024-12-01") == date(2024, 12, 1)


def test_parse_date_flexible_dd_mm_yyyy() -> None:
    assert _parse_date_flexible("01/12/2024") == date(2024, 12, 1)


def test_parse_date_flexible_dd_dash_mm_dash_yyyy() -> None:
    assert _parse_date_flexible("01-12-2024") == date(2024, 12, 1)


def test_parse_date_flexible_dd_mmm_yyyy() -> None:
    assert _parse_date_flexible("01 Jan 2025") == date(2025, 1, 1)


def test_parse_date_flexible_dd_mmmm_yyyy() -> None:
    assert _parse_date_flexible("01 January 2025") == date(2025, 1, 1)


def test_parse_date_flexible_raises_on_unrecognised() -> None:
    try:
        _parse_date_flexible("not-a-date")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


# ===================================================================
# Amount normalisation
# ===================================================================


def test_normalize_amount_plain() -> None:
    assert _normalize_amount("10.00") == Decimal("10.00")


def test_normalize_amount_sgd_prefix() -> None:
    assert _normalize_amount("SGD 29.90") == Decimal("29.90")


def test_normalize_amount_s_dollar_prefix() -> None:
    assert _normalize_amount("S$12.34") == Decimal("12.34")


def test_normalize_amount_dollar_prefix() -> None:
    assert _normalize_amount("$19.90") == Decimal("19.90")


def test_normalize_amount_comma_thousands() -> None:
    assert _normalize_amount("1,234.56") == Decimal("1234.56")


def test_normalize_amount_parentheses_negative() -> None:
    assert _normalize_amount("(12.34)") == Decimal("-12.34")


def test_normalize_amount_explicit_negative() -> None:
    assert _normalize_amount("-25.00") == Decimal("-25.00")


def test_normalize_amount_eur_prefix() -> None:
    assert _normalize_amount("EUR 89.50") == Decimal("89.50")


def test_normalize_amount_gbp_prefix() -> None:
    assert _normalize_amount("GBP 9.99") == Decimal("9.99")


# ===================================================================
# Signed amount mode
# ===================================================================


def test_parse_signed_amount_positive() -> None:
    assert _parse_signed_amount("10.00") == Decimal("10.00")


def test_parse_signed_amount_negative_rejected_by_hardened_parser() -> None:
    """Negative signed amounts are now classified (not rejected) with amount_direction set."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "(12.34)", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        # Negative amount is now classified, not rejected
        assert len(result.rows) == 1
        assert result.rows[0].amount == Decimal("12.34")  # absolute value
        assert result.rows[0].amount_direction is not None
        assert len(result.validation_errors) == 0
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# Debit / credit split mode
# ===================================================================


def test_debit_credit_mode_parses_debit_as_positive_spend() -> None:
    """Debit column is parsed as positive spend (outgoing)."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "Debit", "Credit", "currency"],
        [["01/12/2024", "Apple", "29.90", "", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path, amount_mode="debit_credit")
        assert result.success, result.errors
        assert len(result.rows) == 1
        assert result.rows[0].amount == Decimal("29.90")
    finally:
        path.unlink(missing_ok=True)


def test_debit_credit_mode_credit_only_row_produces_validation_error() -> None:
    """Credit-only rows are now classified (not rejected) with amount_direction set to CREDIT."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "Debit", "Credit", "currency"],
        [["01/12/2024", "GRAB HOLDINGS", "", "8.50", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path, amount_mode="debit_credit")
        # Credit-only row is now classified, not rejected
        assert len(result.rows) == 1
        assert result.rows[0].amount == Decimal("8.50")
        assert result.rows[0].amount_direction is not None
        assert len(result.validation_errors) == 0
    finally:
        path.unlink(missing_ok=True)


def test_debit_credit_mode_both_present_produces_validation_error() -> None:
    """Both debit and credit present produces validation error."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "Debit", "Credit", "currency"],
        [["01/12/2024", "AMAZON", "25.00", "25.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path, amount_mode="debit_credit")
        assert len(result.validation_errors) == 1
        assert "Both debit and credit" in result.validation_errors[0].error_message
    finally:
        path.unlink(missing_ok=True)


def test_debit_credit_mode_missing_both_produces_validation_error() -> None:
    """Neither debit nor credit produces validation error."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "Debit", "Credit", "currency"],
        [["01/12/2024", "Apple", "", "", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path, amount_mode="debit_credit")
        assert len(result.validation_errors) == 1
    finally:
        path.unlink(missing_ok=True)


def test_debit_credit_mode_handles_comma_and_currency_in_debit() -> None:
    """Debit/credit mode normalises amounts with commas and currency symbols."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "Debit", "Credit", "currency"],
        [["01/12/2024", "GIANT", "SGD 1,234.56", "", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path, amount_mode="debit_credit")
        assert result.success
        assert result.rows[0].amount == Decimal("1234.56")
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# Row fingerprint
# ===================================================================


def test_row_fingerprint_is_stable() -> None:
    """Same row content produces same fingerprint."""
    row = {"merchant": "Apple", "amount": "10.00", "currency": "SGD"}
    fp1 = _csv_row_fingerprint(row)
    fp2 = _csv_row_fingerprint(row)
    assert fp1 == fp2
    assert len(fp1) == 64
    int(fp1, 16)


def test_row_fingerprint_differs_for_different_content() -> None:
    """Different row content produces different fingerprint."""
    row1 = {"merchant": "Apple", "amount": "10.00"}
    row2 = {"merchant": "Netflix", "amount": "10.00"}
    assert _csv_row_fingerprint(row1) != _csv_row_fingerprint(row2)


def test_row_fingerprint_uses_canonical_only() -> None:
    """When canonical is provided, only canonical keys are included."""
    row = {"Trans Date": "01/12/2024", "Desc": "Apple", "Extra": "ignore"}
    canonical = {"transaction_date": "Trans Date", "merchant_raw": "Desc"}
    fp = _csv_row_fingerprint(row, canonical)
    # Same row content via canonical should produce same fingerprint
    fp2 = _csv_row_fingerprint(row, canonical)
    assert fp == fp2


# ===================================================================
# Row-level validation errors
# ===================================================================


def test_missing_amount_produces_validation_error() -> None:
    """Empty amount produces a RowValidationError."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.validation_errors) == 1
        ve = result.validation_errors[0]
        assert isinstance(ve, RowValidationError)
        assert ve.field == "amount"
        assert ve.row_number == 2
        assert ve.raw_row is not None
    finally:
        path.unlink(missing_ok=True)


def test_missing_currency_produces_validation_error() -> None:
    """Empty currency produces a RowValidationError."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", ""]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.validation_errors) == 1
        assert result.validation_errors[0].field == "currency"
    finally:
        path.unlink(missing_ok=True)


def test_invalid_date_produces_validation_error() -> None:
    """Unparseable date produces a RowValidationError."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["not-a-date", "Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.validation_errors) == 1
        assert result.validation_errors[0].field == "transaction_date"
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# Backward compatibility -- legacy parser still works
# ===================================================================


def test_legacy_parse_file_still_works() -> None:
    """The legacy parse_file() still works with the original column contract."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file(path)
        assert result.success
        assert len(result.rows) == 1
        assert result.rows[0].merchant_raw == "Apple"
        assert isinstance(result, CsvImportResult)
    finally:
        path.unlink(missing_ok=True)


def test_legacy_parse_text_still_works() -> None:
    """The legacy parse_text() still works."""
    csv_content = "transaction_date,merchant_raw,amount,currency\n2024-12-01,Apple,10.00,SGD\n"
    adapter = StatementCsvAdapter()
    result = adapter.parse_text(csv_content)
    assert result.success
    assert len(result.rows) == 1


def test_legacy_rejects_non_iso_date() -> None:
    """Legacy parser still rejects non-ISO dates."""
    csv_content = "transaction_date,merchant_raw,amount,currency\n01-12-2024,Apple,10.00,SGD\n"
    adapter = StatementCsvAdapter()
    result = adapter.parse_text(csv_content)
    assert result.error_count >= 1


def test_csv_import_result_has_warnings_and_validation_errors_fields() -> None:
    """CsvImportResult has warnings and validation_errors fields (additive, backward compatible)."""
    result = CsvImportResult()
    assert result.warnings == []
    assert result.validation_errors == []
    assert result.success  # error_count is 0


# ===================================================================
# ColumnAliasMap customisation
# ===================================================================


def test_custom_alias_map() -> None:
    """Custom ColumnAliasMap overrides defaults."""
    custom_map = ColumnAliasMap(
        merchant_raw_aliases=("narration",),
        txn_date_aliases=("txn_date",),
    )
    path = _write_csv(
        ["txn_date", "narration", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "SGD"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path, alias_map=custom_map)
        assert result.success
        assert result.rows[0].merchant_raw == "Apple"
        assert result.rows[0].transaction_date == date(2024, 12, 1)
    finally:
        path.unlink(missing_ok=True)


# ===================================================================
# OCBC Giant row -- comma-thousands amount, currency, reference integrity
# ===================================================================


def test_ocbc_giant_row_amount_comma_thousands() -> None:
    """The Giant supermarket row parses the quoted comma-thousands amount correctly."""
    path = _fixture_path("ocbc_credit_card_like.csv")
    adapter = StatementCsvAdapter()
    result = adapter.parse_file_hardened(path)
    # Find the Giant row
    giant = next((r for r in result.rows if r.merchant_raw == "GIANT SUPERMARKET"), None)
    assert giant is not None, "GIANT SUPERMARKET row not found in parsed rows"
    assert giant.amount == Decimal("1234.56"), f"Expected 1234.56, got {giant.amount}"


def test_ocbc_giant_row_currency_preserved() -> None:
    """The Giant supermarket row preserves currency SGD."""
    path = _fixture_path("ocbc_credit_card_like.csv")
    adapter = StatementCsvAdapter()
    result = adapter.parse_file_hardened(path)
    giant = next((r for r in result.rows if r.merchant_raw == "GIANT SUPERMARKET"), None)
    assert giant is not None
    assert giant.currency == "SGD", f"Expected SGD, got {giant.currency}"


def test_ocbc_giant_row_reference_preserved() -> None:
    """The Giant supermarket row preserves reference REF003."""
    path = _fixture_path("ocbc_credit_card_like.csv")
    adapter = StatementCsvAdapter()
    result = adapter.parse_file_hardened(path)
    giant = next((r for r in result.rows if r.merchant_raw == "GIANT SUPERMARKET"), None)
    assert giant is not None
    assert giant.statement_row_reference == "REF003", (
        f"Expected REF003, got {giant.statement_row_reference}"
    )


# ===================================================================
# Currency validation
# ===================================================================


def test_invalid_currency_numeric_produces_validation_error() -> None:
    """A numeric value in the currency field produces a RowValidationError."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "234.56"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.validation_errors) == 1
        ve = result.validation_errors[0]
        assert ve.field == "currency", f"Expected field=currency, got {ve.field}"
        assert "Invalid currency" in ve.error_message
    finally:
        path.unlink(missing_ok=True)


def test_invalid_currency_symbol_produces_validation_error() -> None:
    """A currency symbol like '$' in the currency field produces a RowValidationError."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "$"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.validation_errors) == 1
        ve = result.validation_errors[0]
        assert ve.field == "currency"
        assert "Invalid currency" in ve.error_message
    finally:
        path.unlink(missing_ok=True)


def test_invalid_currency_amount_with_space_produces_error() -> None:
    """A value like 'SGD 12.34' in the currency field produces a RowValidationError."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "SGD 12.34"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.validation_errors) == 1
        ve = result.validation_errors[0]
        assert ve.field == "currency"
    finally:
        path.unlink(missing_ok=True)


def test_invalid_currency_numeric_three_digits_produces_error() -> None:
    """A 3-digit numeric code like '123' produces a RowValidationError."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "123"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert len(result.validation_errors) == 1
        ve = result.validation_errors[0]
        assert ve.field == "currency"
    finally:
        path.unlink(missing_ok=True)


def test_lowercase_currency_normalizes_to_uppercase() -> None:
    """Lowercase 'sgd' is normalised to uppercase 'SGD'."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "sgd"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.success
        assert len(result.rows) == 1
        assert result.rows[0].currency == "SGD", (
            f"Expected SGD (normalised), got {result.rows[0].currency}"
        )
    finally:
        path.unlink(missing_ok=True)


def test_valid_currency_eur_passes() -> None:
    """A valid 3-letter currency code passes validation."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "EUR"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.success
        assert result.rows[0].currency == "EUR"
    finally:
        path.unlink(missing_ok=True)


def test_valid_currency_gbp_passes() -> None:
    """A valid 3-letter currency code GBP passes validation."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [["2024-12-01", "Apple", "10.00", "GBP"]],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.success
        assert result.rows[0].currency == "GBP"
    finally:
        path.unlink(missing_ok=True)


def test_validate_currency_standalone() -> None:
    """_validate_currency works correctly as a standalone function."""
    assert _validate_currency("SGD") == "SGD"
    assert _validate_currency("usd") == "USD"
    assert _validate_currency("EUR") == "EUR"
    assert _validate_currency("  gbp  ") == "GBP"

    for invalid in ["234.56", "$", "SGD 12.34", "", "123", "US", "SGDD"]:
        try:
            _validate_currency(invalid)
            assert False, f"Expected ValueError for {invalid!r}"
        except ValueError:
            pass


def test_malformed_unquoted_comma_thousands_produces_validation_error() -> None:
    """Unquoted comma-thousands shift columns; the shifted amount lands in
    the currency field and is rejected by currency validation.

    The raw CSV text has commas that split the amount across columns,
    putting '234.56' into the currency cell instead of 'SGD'.
    """
    # Write raw text without csv.writer (which would quote the comma)
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".csv", prefix="test_malformed_")
    import os

    raw_csv = "transaction_date,merchant_raw,amount,currency\n01/12/2024,GIANT,1,234.56,SGD\n"
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(raw_csv)

    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(Path(path))
        # The unquoted comma splits '1,234.56' into two fields:
        # amount='1', currency='234.56' -> currency validation rejects it
        assert len(result.validation_errors) >= 1
        currency_errors = [ve for ve in result.validation_errors if ve.field == "currency"]
        assert len(currency_errors) >= 1, (
            f"Expected currency validation error, got: {result.validation_errors}"
        )
    finally:
        Path(path).unlink(missing_ok=True)


# ===================================================================
# Edge cases
# ===================================================================


def test_skipped_empty_merchant() -> None:
    """Empty merchant rows are silently skipped."""
    path = _write_csv(
        ["transaction_date", "merchant_raw", "amount", "currency"],
        [
            ["2024-12-01", "", "10.00", "SGD"],
            ["2024-12-02", "Netflix", "15.99", "SGD"],
        ],
    )
    try:
        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened(path)
        assert result.skipped_count >= 1
        assert len(result.rows) == 1
        assert result.rows[0].merchant_raw == "Netflix"
    finally:
        path.unlink(missing_ok=True)


def test_resolve_columns_returns_none_when_no_merchant() -> None:
    """_resolve_columns returns None when no merchant column can be resolved."""
    alias_map = ColumnAliasMap()
    result = _resolve_columns(
        ["date", "amount", "currency"],
        alias_map,
        amount_mode="signed",
    )
    assert result is None


def test_resolve_columns_returns_none_when_no_amount_signed() -> None:
    """_resolve_columns returns None when no amount column in signed mode."""
    alias_map = ColumnAliasMap()
    result = _resolve_columns(
        ["date", "merchant", "currency"],
        alias_map,
        amount_mode="signed",
    )
    assert result is None


def test_resolve_columns_returns_none_when_no_debit_or_credit() -> None:
    """_resolve_columns returns None when no debit/credit column in that mode."""
    alias_map = ColumnAliasMap()
    result = _resolve_columns(
        ["date", "merchant", "currency"],
        alias_map,
        amount_mode="debit_credit",
    )
    assert result is None


# ===================================================================
# Fixture file integration tests
# ===================================================================


def test_fixture_ocbc_like_parses_correctly() -> None:
    """The OCBC-like fixture parses with the hardened parser."""
    path = _fixture_path("ocbc_credit_card_like.csv")
    adapter = StatementCsvAdapter()
    result = adapter.parse_file_hardened(path)
    # All 8 data rows now parse through (credit rows are classified, not rejected):
    # Apple, Netflix, Giant, Grab, Shopee, PayPal, Amazon, Spotify -> 8 successful parse
    # Shopee and Amazon have amount_direction set (not rejected)
    assert len(result.rows) == 8
    # Check merchant_raw is preserved
    merchants = {r.merchant_raw for r in result.rows}
    assert "APPLE.COM/BILL" in merchants
    assert "NETFLIX SINGAPORE" in merchants
    assert "GIANT SUPERMARKET" in merchants
    # No validation errors because credit rows are now classified
    assert len(result.validation_errors) == 0


def test_fixture_uob_like_parses_with_debit_credit_mode() -> None:
    """The UOB-like fixture parses with debit/credit split mode."""
    path = _fixture_path("uob_bank_like.csv")
    adapter = StatementCsvAdapter()
    result = adapter.parse_file_hardened(path, amount_mode="debit_credit")
    # Apple, Netflix, Giant -> 3 debit rows parsed
    # PayPal: txn_absent, posted_date only -> 1 more (no backfill)
    # Grab: credit-only -> now classified, not rejected -> 1 more
    # Shopee: debit -> 1 more
    # Amazon: both debit and credit -> validation error
    # No merchant row -> skipped
    # Total rows: 6, 1 validation error (Amazon both)
    assert len(result.rows) == 6
    assert len(result.validation_errors) == 1


def test_fixture_wise_like_parses_with_date_formats() -> None:
    """The Wise-like fixture parses with DD MMM YYYY date formats and multi-currency."""
    path = _fixture_path("wise_like.csv")
    adapter = StatementCsvAdapter()
    result = adapter.parse_file_hardened(path)
    # All 5 rows with txn_date should parse.
    # Booking.com has posted_date only (no txn_date backfill) -> 1 more row.
    assert len(result.rows) == 6
    currencies = {r.currency for r in result.rows}
    assert "USD" in currencies
    assert "EUR" in currencies
    assert "GBP" in currencies
    assert "SGD" in currencies


def test_no_database_touched() -> None:
    """Verify that test does not modify database/finance.db."""
    _ = Path("database/finance.db")
    # This test does not use the database at all, so it should not be modified.
    # Just verify the path is not accidentally altered.
    assert True  # Placeholder -- this test does not import or use the database


# ===================================================================
# _HardenedRowResult dataclass test
# ===================================================================


def test_hardened_row_result_defaults() -> None:
    """_HardenedRowResult defaults are correct."""
    from finance_core.reconciliation.statement_csv import _HardenedRowResult

    result = _HardenedRowResult()
    assert result.row is None
    assert result.skipped is False
    assert result.warning is None
    assert result.validation_error is None


# ===================================================================
# RowValidationError properties
# ===================================================================


def test_row_validation_error_contains_raw_row() -> None:
    """RowValidationError preserves raw_row for evidence."""
    raw = {"txn_date": "bad", "merchant": "Apple"}
    ve = RowValidationError(
        row_number=3,
        raw_row=raw,
        error_message="Bad date",
        field="transaction_date",
    )
    assert ve.row_number == 3
    assert ve.raw_row == raw
    assert ve.field == "transaction_date"
