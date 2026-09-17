"""Tests for PDF Statement Bridge Runtime Helper v1.

Verifies that the PDF statement bridge helper is deterministic, read-only,
correctly handles date separation, amount normalization, fingerprint
generation, validation, and blocked-state handling. Uses synthetic data
only — no real PDFs, no OCR, no live DB.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import (
    ParsedPdfStatementRow,
    PdfBlockedReason,
    PdfBlockedStatementRow,
    PdfValidationResult,
    build_pdf_statement_row_fingerprint,
    normalize_pdf_statement_row,
    normalize_pdf_statement_row_checked,
    normalize_pdf_statement_rows_batch,
    validate_pdf_statement_row,
)
from finance_core.reconciliation.pdf_statement_evidence import (
    PdfDirectionConfidence,
    PdfDirectionSource,
    PdfOriginalAmountSign,
    PdfRowReviewStatus,
)
from finance_core.reconciliation.statement_import_contracts import StructuredStatementRow

# ---------------------------------------------------------------------------
# Synthetic row factory
# ---------------------------------------------------------------------------


def _make_row(
    *,
    source_statement_id: str = "stmt-001",
    attachment_path: str = "/tmp/fake.pdf",
    description: str = "Test Merchant",
    amount: Decimal | None = Decimal("100.00"),
    currency: str = "SGD",
    source_page_number: int | None = 1,
    source_row_ref: str | None = "page-1:line-1",
    transaction_date: date | None = date(2026, 6, 1),
    posted_date: date | None = None,
    raw_row_text: str = "01/06/2026 Test Merchant 100.00 DEBIT",
    external_reference: str | None = None,
    amount_direction: StatementAmountDirection = StatementAmountDirection.DEBIT,
) -> ParsedPdfStatementRow:
    original_token = str(amount) if amount is not None else None
    original_sign = (
        PdfOriginalAmountSign.MISSING
        if amount is None
        else PdfOriginalAmountSign.ZERO
        if amount == 0
        else PdfOriginalAmountSign.NEGATIVE
        if amount < 0
        else PdfOriginalAmountSign.POSITIVE
    )
    return ParsedPdfStatementRow(
        source_statement_id=source_statement_id,
        attachment_path=attachment_path,
        description=description,
        amount=amount,
        currency=currency,
        source_page_number=source_page_number,
        source_row_ref=source_row_ref,
        transaction_date=transaction_date,
        posted_date=posted_date,
        raw_row_text=raw_row_text,
        external_reference=external_reference,
        amount_direction=amount_direction,
        source_content_hash="a" * 64,
        source_row_number=1,
        source_text_excerpt=raw_row_text,
        direction_source=PdfDirectionSource.EXPLICIT_TOKEN,
        direction_confidence=PdfDirectionConfidence.HIGH,
        review_status=PdfRowReviewStatus.AUTHORITATIVE,
        original_amount_token=original_token,
        original_amount_sign=original_sign,
        currency_token=currency,
        transaction_date_token=transaction_date.isoformat() if transaction_date else None,
        posted_date_token=posted_date.isoformat() if posted_date else None,
    )


# ===================================================================
# 1. Date separation
# ===================================================================


class TestDateSeparation:
    """Both transaction_date and posted_date must be preserved independently."""

    def test_both_dates_preserved(self) -> None:
        row = _make_row(
            transaction_date=date(2026, 6, 28),
            posted_date=date(2026, 6, 29),
        )
        result = normalize_pdf_statement_row(row)
        assert result.transaction_date == date(2026, 6, 28)
        assert result.posted_date == date(2026, 6, 29)

    def test_posted_only_date_preserved(self) -> None:
        row = _make_row(
            transaction_date=None,
            posted_date=date(2026, 6, 29),
        )
        result = normalize_pdf_statement_row(row)
        assert result.transaction_date is None
        assert result.posted_date == date(2026, 6, 29)

    def test_transaction_only_date_preserved(self) -> None:
        row = _make_row(
            transaction_date=date(2026, 6, 28),
            posted_date=None,
        )
        result = normalize_pdf_statement_row(row)
        assert result.transaction_date == date(2026, 6, 28)
        assert result.posted_date is None

    def test_neither_date_fails_closed(self) -> None:
        row = _make_row(transaction_date=None, posted_date=None)
        with pytest.raises(ValueError, match="missing_usable_date"):
            normalize_pdf_statement_row(row)

    def test_posted_date_not_collapsed_into_transaction_date(self) -> None:
        """posted_date must not be used to fill in missing transaction_date."""
        row = _make_row(
            transaction_date=None,
            posted_date=date(2026, 7, 1),
        )
        result = normalize_pdf_statement_row(row)
        assert result.transaction_date is None
        assert result.posted_date == date(2026, 7, 1)


# ===================================================================
# 2. Amount normalization
# ===================================================================


class TestAmountNormalization:
    """Amounts must be normalized to non-negative Decimal."""

    def test_debit_preserves_positive_amount(self) -> None:
        row = _make_row(amount=Decimal("42.50"), amount_direction=StatementAmountDirection.DEBIT)
        result = normalize_pdf_statement_row(row)
        assert result.amount == Decimal("42.50")
        assert result.amount_direction == StatementAmountDirection.DEBIT

    def test_credit_preserves_positive_amount(self) -> None:
        row = _make_row(amount=Decimal("100.00"), amount_direction=StatementAmountDirection.CREDIT)
        result = normalize_pdf_statement_row(row)
        assert result.amount == Decimal("100.00")
        assert result.amount_direction == StatementAmountDirection.CREDIT

    def test_negative_normalized_amount_is_rejected(self) -> None:
        row = _make_row(amount=Decimal("-50.00"), amount_direction=StatementAmountDirection.DEBIT)
        with pytest.raises(ValueError, match="invalid_amount"):
            normalize_pdf_statement_row(row)

    def test_refund_preserves_amount(self) -> None:
        row = _make_row(amount=Decimal("25.00"), amount_direction=StatementAmountDirection.REFUND)
        result = normalize_pdf_statement_row(row)
        assert result.amount == Decimal("25.00")
        assert result.amount_direction == StatementAmountDirection.REFUND

    def test_payment_preserves_amount(self) -> None:
        row = _make_row(amount=Decimal("500.00"), amount_direction=StatementAmountDirection.PAYMENT)
        result = normalize_pdf_statement_row(row)
        assert result.amount == Decimal("500.00")
        assert result.amount_direction == StatementAmountDirection.PAYMENT

    def test_fee_preserves_amount(self) -> None:
        row = _make_row(amount=Decimal("10.00"), amount_direction=StatementAmountDirection.FEE)
        result = normalize_pdf_statement_row(row)
        assert result.amount == Decimal("10.00")
        assert result.amount_direction == StatementAmountDirection.FEE

    def test_unspecified_interest_requires_review(self) -> None:
        row = _make_row(amount=Decimal("2.50"), amount_direction=StatementAmountDirection.INTEREST)
        with pytest.raises(ValueError, match="unknown_direction"):
            normalize_pdf_statement_row(row)

    def test_unknown_direction_fails_closed(self) -> None:
        row = _make_row(amount=Decimal("99.00"), amount_direction=StatementAmountDirection.UNKNOWN)
        with pytest.raises(ValueError, match="unknown_direction"):
            normalize_pdf_statement_row(row)

    def test_raw_amount_is_string(self) -> None:
        row = _make_row(amount=Decimal("42.50"))
        result = normalize_pdf_statement_row(row)
        assert result.raw_amount == "42.50"


# ===================================================================
# 3. Fingerprint
# ===================================================================


class TestFingerprint:
    """Fingerprints must be deterministic and exclude volatile fields."""

    def test_fingerprint_format(self) -> None:
        row = _make_row()
        fp = build_pdf_statement_row_fingerprint(row)
        assert len(fp) == 64
        int(fp, 16)

    def test_identical_rows_same_fingerprint(self) -> None:
        a = _make_row()
        b = _make_row()
        assert build_pdf_statement_row_fingerprint(a) == build_pdf_statement_row_fingerprint(b)

    def test_different_amount_different_fingerprint(self) -> None:
        a = _make_row(amount=Decimal("100.00"))
        b = _make_row(amount=Decimal("200.00"))
        assert build_pdf_statement_row_fingerprint(a) != build_pdf_statement_row_fingerprint(b)

    def test_different_description_different_fingerprint(self) -> None:
        a = _make_row(description="A")
        b = _make_row(description="B")
        assert build_pdf_statement_row_fingerprint(a) != build_pdf_statement_row_fingerprint(b)

    def test_different_date_different_fingerprint(self) -> None:
        a = _make_row(transaction_date=date(2026, 6, 1))
        b = _make_row(transaction_date=date(2026, 6, 2))
        assert build_pdf_statement_row_fingerprint(a) != build_pdf_statement_row_fingerprint(b)

    def test_different_currency_different_fingerprint(self) -> None:
        a = _make_row(currency="SGD")
        b = _make_row(currency="USD")
        assert build_pdf_statement_row_fingerprint(a) != build_pdf_statement_row_fingerprint(b)

    def test_different_amount_direction_different_fingerprint(self) -> None:
        a = _make_row(amount=Decimal("10.00"), amount_direction=StatementAmountDirection.DEBIT)
        b = _make_row(amount=Decimal("10.00"), amount_direction=StatementAmountDirection.CREDIT)
        assert build_pdf_statement_row_fingerprint(a) != build_pdf_statement_row_fingerprint(b)

    def test_different_page_different_fingerprint(self) -> None:
        a = _make_row(source_page_number=1)
        b = _make_row(source_page_number=2)
        assert build_pdf_statement_row_fingerprint(a) != build_pdf_statement_row_fingerprint(b)

    def test_different_row_ref_different_fingerprint(self) -> None:
        a = _make_row(source_row_ref="r1")
        b = _make_row(source_row_ref="r2")
        assert build_pdf_statement_row_fingerprint(a) != build_pdf_statement_row_fingerprint(b)

    def test_fingerprint_excludes_raw_row_text(self) -> None:
        a = _make_row(raw_row_text="foo")
        b = _make_row(raw_row_text="bar")
        assert build_pdf_statement_row_fingerprint(a) == build_pdf_statement_row_fingerprint(b)

    def test_fingerprint_excludes_timestamps(self) -> None:
        """Fingerprint includes no time-dependent fields."""
        row = _make_row()
        fp = build_pdf_statement_row_fingerprint(row)
        # Two calls same second
        fp2 = build_pdf_statement_row_fingerprint(row)
        assert fp == fp2

    def test_fingerprint_in_normalized_row(self) -> None:
        row = _make_row()
        result = normalize_pdf_statement_row(row)
        assert result.row_fingerprint is not None
        assert len(result.row_fingerprint) == 64
        int(result.row_fingerprint, 16)
        assert result.row_fingerprint == build_pdf_statement_row_fingerprint(row)


# ===================================================================
# 4. Validation
# ===================================================================


class TestValidation:
    """Validation must correctly identify blocked rows."""

    def test_valid_row_passes(self) -> None:
        row = _make_row(
            amount=Decimal("100.00"),
            currency="SGD",
            description="Valid",
            transaction_date=date(2026, 6, 1),
        )
        result = validate_pdf_statement_row(row)
        assert result.is_valid is True
        assert result.is_blocked is False
        assert len(result.blocked_reasons) == 0

    def test_missing_amount_blocks(self) -> None:
        row = _make_row(amount=None, transaction_date=date(2026, 6, 1))
        result = validate_pdf_statement_row(row)
        assert result.is_valid is False
        assert PdfBlockedReason.MISSING_AMOUNT in result.blocked_reasons

    def test_unknown_direction_blocks(self) -> None:
        row = _make_row(
            amount=Decimal("100.00"),
            currency="SGD",
            description="Test",
            transaction_date=date(2026, 6, 1),
            amount_direction=StatementAmountDirection.UNKNOWN,
        )
        result = validate_pdf_statement_row(row)
        assert result.is_valid is False
        assert PdfBlockedReason.UNKNOWN_DIRECTION in result.blocked_reasons

    def test_missing_currency_blocks(self) -> None:
        row = _make_row(currency="", amount=Decimal("100.00"))
        result = validate_pdf_statement_row(row)
        assert result.is_valid is False
        assert PdfBlockedReason.MISSING_CURRENCY in result.blocked_reasons

    def test_short_currency_blocks(self) -> None:
        row = _make_row(currency="SG", amount=Decimal("100.00"))
        result = validate_pdf_statement_row(row)
        assert result.is_valid is False
        assert PdfBlockedReason.MISSING_CURRENCY in result.blocked_reasons

    def test_non_iso_currency_blocks(self) -> None:
        row = _make_row(currency="US$", amount=Decimal("100.00"))
        result = validate_pdf_statement_row(row)
        assert result.is_valid is False
        assert PdfBlockedReason.MISSING_CURRENCY in result.blocked_reasons

    def test_valid_currency_is_normalized(self) -> None:
        row = _make_row(
            currency=" sgd ",
            amount=Decimal("100.00"),
            transaction_date=date(2026, 6, 1),
        )
        result = normalize_pdf_statement_row_checked(row)
        assert result.currency == "SGD"

    def test_whitespace_only_currency_blocks(self) -> None:
        row = _make_row(currency="   ", amount=Decimal("100.00"))
        result = validate_pdf_statement_row(row)
        assert result.is_valid is False
        assert PdfBlockedReason.MISSING_CURRENCY in result.blocked_reasons

    def test_neither_date_blocks(self) -> None:
        row = _make_row(
            amount=Decimal("100.00"),
            currency="SGD",
            description="Test",
            transaction_date=None,
            posted_date=None,
        )
        result = validate_pdf_statement_row(row)
        assert result.is_valid is False
        assert PdfBlockedReason.MISSING_USABLE_DATE in result.blocked_reasons

    def test_one_date_is_enough(self) -> None:
        row = _make_row(
            amount=Decimal("100.00"),
            currency="SGD",
            description="Test",
            transaction_date=date(2026, 6, 1),
            posted_date=None,
        )
        result = validate_pdf_statement_row(row)
        assert result.is_valid is True

    def test_missing_description_blocks(self) -> None:
        row = _make_row(description="", amount=Decimal("100.00"))
        result = validate_pdf_statement_row(row)
        assert result.is_valid is False
        assert PdfBlockedReason.MISSING_DESCRIPTION in result.blocked_reasons

    def test_whitespace_description_blocks(self) -> None:
        row = _make_row(description="   ", amount=Decimal("100.00"))
        result = validate_pdf_statement_row(row)
        assert result.is_valid is False
        assert PdfBlockedReason.MISSING_DESCRIPTION in result.blocked_reasons

    def test_multiple_blocked_reasons(self) -> None:
        row = _make_row(
            amount=Decimal("100.00"),
            currency="",
            description="",
            amount_direction=StatementAmountDirection.UNKNOWN,
        )
        result = validate_pdf_statement_row(row)
        assert len(result.blocked_reasons) >= 3
        assert PdfBlockedReason.MISSING_CURRENCY in result.blocked_reasons
        assert PdfBlockedReason.MISSING_DESCRIPTION in result.blocked_reasons
        assert PdfBlockedReason.UNKNOWN_DIRECTION in result.blocked_reasons

    def test_validation_result_is_frozen(self) -> None:
        result = validate_pdf_statement_row(_make_row())
        with pytest.raises(Exception):
            result.is_valid = False  # type: ignore[misc]


# ===================================================================
# 5. Evidence preservation
# ===================================================================


class TestEvidencePreservation:
    """Raw evidence must be preserved in raw_row_payload."""

    def test_attachment_path_preserved(self) -> None:
        row = _make_row(attachment_path="/tmp/stmt.pdf")
        result = normalize_pdf_statement_row(row)
        assert result.raw_row_payload is not None
        assert result.raw_row_payload["attachment_path"] == "/tmp/stmt.pdf"

    def test_raw_row_text_preserved(self) -> None:
        row = _make_row(raw_row_text="TRANSFER TO ACCT 123")
        result = normalize_pdf_statement_row(row)
        assert result.raw_row_payload["raw_row_text"] == "TRANSFER TO ACCT 123"

    def test_source_page_number_preserved(self) -> None:
        row = _make_row(source_page_number=5)
        result = normalize_pdf_statement_row(row)
        assert result.raw_row_payload.get("source_page_number") == 5

    def test_source_row_ref_preserved(self) -> None:
        row = _make_row(source_row_ref="row-42")
        result = normalize_pdf_statement_row(row)
        assert result.raw_row_payload.get("source_row_ref") == "row-42"

    def test_source_row_ref_preserved_in_statement_row_reference(self) -> None:
        row = _make_row(source_row_ref="row-42")
        result = normalize_pdf_statement_row(row)
        assert result.statement_row_reference == "row-42"

    def test_evidence_excludes_none_values(self) -> None:
        row = _make_row(source_page_number=None, source_row_ref=None)
        result = validate_pdf_statement_row(row)
        assert PdfBlockedReason.MISSING_PAGE_EVIDENCE in result.blocked_reasons
        assert PdfBlockedReason.MISSING_ROW_LOCATOR in result.blocked_reasons


# ===================================================================
# 6. StructuredStatementRow compatibility
# ===================================================================


class TestStructuredStatementRowCompatibility:
    """Normalized rows must be compatible with StructuredStatementRow."""

    def test_result_is_structured_statement_row(self) -> None:
        row = _make_row()
        result = normalize_pdf_statement_row(row)
        assert isinstance(result, StructuredStatementRow)

    def test_merchant_raw_from_description(self) -> None:
        row = _make_row(description="Starbucks Coffee")
        result = normalize_pdf_statement_row(row)
        assert result.merchant_raw == "Starbucks Coffee"

    def test_row_fingerprint_is_set(self) -> None:
        row = _make_row()
        result = normalize_pdf_statement_row(row)
        assert result.row_fingerprint is not None
        assert len(result.row_fingerprint) == 64
        int(result.row_fingerprint, 16)

    def test_external_reference_not_mapped_to_structured_row(self) -> None:
        """external_reference is retained in ParsedPdfStatementRow but not
        mapped to StructuredStatementRow directly (evidence only)."""
        row = _make_row(external_reference="ext-123")
        result = normalize_pdf_statement_row(row)
        assert result.row_fingerprint is not None


# ===================================================================
# 7. Immutability
# ===================================================================


class TestImmutability:
    """All exported dataclasses must be frozen."""

    def test_parsed_pdf_statement_row_is_frozen(self) -> None:
        row = _make_row()
        with pytest.raises(Exception):
            row.amount = Decimal("999")  # type: ignore[misc]

    def test_pdf_validation_result_is_frozen(self) -> None:
        result = PdfValidationResult(is_valid=True)
        with pytest.raises(Exception):
            result.is_valid = False  # type: ignore[misc]

    def test_structured_statement_row_is_frozen(self) -> None:
        row = _make_row()
        result = normalize_pdf_statement_row(row)
        with pytest.raises(Exception):
            result.amount = Decimal("999")  # type: ignore[misc]


# ===================================================================
# 8. No live database reference
# ===================================================================


class TestNoLiveDatabaseReference:
    """Module must not reference database/finance.db or sqlite3."""

    def test_no_live_db_path_in_module(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_bridge as psb

        source = inspect.getsource(psb)
        assert "database/finance.db" not in source
        assert "finance.db" not in source
        assert "sqlite3" not in source.lower()

    def test_no_pdf_ocr_dependencies(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_bridge as psb

        source = inspect.getsource(psb)
        # Strip the module docstring so non-goal mentions don't trigger.
        import re

        source_no_docstring = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        for dep in (
            "import pdfplumber",
            "import tesseract",
            "import pypdf",
            "import camelot",
            "import tabula",
            "from pdfplumber",
            "from tesseract",
            "from pypdf",
            "from camelot",
            "from tabula",
            "pdfplumber.",
            "tesseract.",
            "pypdf.",
            "camelot.",
            "tabula.",
        ):
            assert dep not in source_no_docstring.lower(), f"Unexpected dependency: {dep}"
        assert "ocr" not in source_no_docstring.lower()


# ===================================================================
# 9. Integration: existing contract tests still pass
# ===================================================================


class TestIntegrationWithContract:
    """The helper must produce results consistent with the contract test."""

    def test_normalized_row_is_valid_structured_row(self) -> None:
        row = _make_row(
            transaction_date=date(2026, 6, 28),
            posted_date=date(2026, 6, 29),
            amount=Decimal("42.50"),
            currency="SGD",
            description="GrabFood",
        )
        result = normalize_pdf_statement_row(row)
        assert result.amount == Decimal("42.50")
        assert result.currency == "SGD"
        assert result.merchant_raw == "GrabFood"
        assert result.transaction_date == date(2026, 6, 28)
        assert result.posted_date == date(2026, 6, 29)
        assert result.amount_direction == StatementAmountDirection.DEBIT
        assert result.raw_amount == "42.50"


# ===================================================================
# 10. Checked normalizer
# ===================================================================


class TestCheckedNormalizer:
    """The checked normalizer must refuse blocked rows."""

    def test_valid_row_normalizes_successfully(self) -> None:
        row = _make_row(
            transaction_date=date(2026, 6, 28),
            amount=Decimal("42.50"),
            currency="SGD",
            description="Valid",
        )
        result = normalize_pdf_statement_row_checked(row)
        assert isinstance(result, StructuredStatementRow)
        assert result.amount == Decimal("42.50")

    def test_blocked_row_raises_value_error(self) -> None:
        row = _make_row(
            amount=Decimal("100.00"),
            currency="",
            description="",
        )
        with pytest.raises(ValueError, match="PDF statement row is blocked"):
            normalize_pdf_statement_row_checked(row)

    def test_blocked_row_message_includes_reasons(self) -> None:
        row = _make_row(
            amount=Decimal("100.00"),
            currency="",
            description="",
            amount_direction=StatementAmountDirection.UNKNOWN,
        )
        with pytest.raises(ValueError, match="unknown_direction"):
            normalize_pdf_statement_row_checked(row)

    def test_checked_matches_unchecked_for_valid_row(self) -> None:
        row = _make_row(
            transaction_date=date(2026, 6, 28),
            amount=Decimal("42.50"),
            currency="SGD",
            description="GrabFood",
        )
        checked = normalize_pdf_statement_row_checked(row)
        unchecked = normalize_pdf_statement_row(row)
        assert checked.amount == unchecked.amount
        assert checked.currency == unchecked.currency
        assert checked.transaction_date == unchecked.transaction_date
        assert checked.row_fingerprint == unchecked.row_fingerprint


# ===================================================================
# 11. Parameterized checked normalizer blocked reason codes
# ===================================================================


class TestCheckedNormalizerBlockedReasons:
    """Checked normalizer must raise ValueError with exact reason code for
    each blocked condition."""

    def _make_row(
        self,
        *,
        amount=Decimal("100.00"),
        currency="SGD",
        description="Test Merchant",
        transaction_date=date(2026, 6, 1),
        amount_direction=StatementAmountDirection.DEBIT,
    ):
        return _make_row(
            amount=amount,
            currency=currency,
            description=description,
            transaction_date=transaction_date,
            amount_direction=amount_direction,
        )

    def _make_row_missing_amount(self):
        """Build a row with amount=None via object.__new__."""
        row = object.__new__(ParsedPdfStatementRow)
        object.__setattr__(row, "source_statement_id", "stmt-001")
        object.__setattr__(row, "attachment_path", "/tmp/test.pdf")
        object.__setattr__(row, "description", "Test Merchant")
        object.__setattr__(row, "amount", None)
        object.__setattr__(row, "currency", "SGD")
        object.__setattr__(row, "source_page_number", None)
        object.__setattr__(row, "source_row_ref", None)
        object.__setattr__(row, "transaction_date", date(2026, 6, 1))
        object.__setattr__(row, "posted_date", None)
        object.__setattr__(row, "raw_row_text", "")
        object.__setattr__(row, "external_reference", None)
        object.__setattr__(row, "amount_direction", StatementAmountDirection.DEBIT)
        return row

    def test_missing_amount_raises_with_reason_code(self) -> None:
        """MISSING_AMOUNT: amount is None."""
        row = self._make_row_missing_amount()
        with pytest.raises(ValueError, match="missing_amount"):
            normalize_pdf_statement_row_checked(row)

    def test_missing_currency_raises_with_reason_code(self) -> None:
        """MISSING_CURRENCY: empty currency, all other fields valid."""
        row = self._make_row(currency="")
        with pytest.raises(ValueError, match="missing_currency"):
            normalize_pdf_statement_row_checked(row)

    def test_missing_usable_date_raises_with_reason_code(self) -> None:
        """MISSING_USABLE_DATE: both dates absent, all other fields valid."""
        row = _make_row(
            amount=Decimal("100.00"),
            currency="SGD",
            description="Test",
            transaction_date=None,
            posted_date=None,
        )
        with pytest.raises(ValueError, match="missing_usable_date"):
            normalize_pdf_statement_row_checked(row)

    def test_missing_description_raises_with_reason_code(self) -> None:
        """MISSING_DESCRIPTION: empty description, all other fields valid."""
        row = self._make_row(description="")
        with pytest.raises(ValueError, match="missing_description"):
            normalize_pdf_statement_row_checked(row)

    def test_unknown_direction_raises_with_reason_code(self) -> None:
        """UNKNOWN_DIRECTION: direction is UNKNOWN, all other fields valid."""
        row = self._make_row(
            amount_direction=StatementAmountDirection.UNKNOWN,
        )
        with pytest.raises(ValueError, match="unknown_direction"):
            normalize_pdf_statement_row_checked(row)


# ===================================================================
# 12. Batch normalization
# ===================================================================


class TestBatchNormalization:
    """Batch normalization must separate valid from blocked rows."""

    def test_all_valid_rows_produces_empty_blocked(self) -> None:
        rows = [
            _make_row(
                source_statement_id="s1",
                transaction_date=date(2026, 6, 1),
                amount=Decimal("50.00"),
                currency="SGD",
                description="Row 1",
            ),
            _make_row(
                source_statement_id="s1",
                transaction_date=date(2026, 6, 2),
                amount=Decimal("100.00"),
                currency="SGD",
                description="Row 2",
            ),
        ]
        result = normalize_pdf_statement_rows_batch(rows)
        assert result.total_rows == 2
        assert result.accepted_count == 2
        assert result.blocked_count == 0
        assert result.has_blocked_rows is False
        assert len(result.accepted_rows) == 2
        assert len(result.blocked_rows) == 0
        assert result.blocked_reason_counts == ()

    def test_all_blocked_rows_produces_empty_accepted(self) -> None:
        rows = [
            _make_row(amount=None, description="Blocked 1", transaction_date=date(2026, 6, 1)),
            _make_row(currency="", description="Blocked 2", amount=Decimal("10.00")),
        ]
        result = normalize_pdf_statement_rows_batch(rows)
        assert result.total_rows == 2
        assert result.accepted_count == 0
        assert result.blocked_count == 2
        assert result.has_blocked_rows is True
        assert len(result.accepted_rows) == 0
        assert len(result.blocked_rows) == 2

    def test_mixed_rows_separates_correctly(self) -> None:
        rows = [
            _make_row(
                source_statement_id="s1",
                transaction_date=date(2026, 6, 1),
                amount=Decimal("50.00"),
                currency="SGD",
                description="Valid",
            ),
            _make_row(
                amount=None,
                currency="SGD",
                description="No Amount",
                transaction_date=date(2026, 6, 1),
            ),
            _make_row(
                source_statement_id="s1",
                transaction_date=date(2026, 6, 2),
                amount=Decimal("100.00"),
                currency="SGD",
                description="Also Valid",
            ),
            _make_row(
                currency="",
                amount=Decimal("10.00"),
                description="No Currency",
                transaction_date=date(2026, 6, 1),
            ),
        ]
        result = normalize_pdf_statement_rows_batch(rows)
        assert result.total_rows == 4
        assert result.accepted_count == 2
        assert result.blocked_count == 2
        assert result.has_blocked_rows is True

    def test_accepted_rows_preserve_input_order(self) -> None:
        rows = [
            _make_row(description="A", amount=Decimal("1.00"), transaction_date=date(2026, 6, 1)),
            _make_row(description="B", amount=Decimal("2.00"), transaction_date=date(2026, 6, 1)),
            _make_row(description="C", amount=Decimal("3.00"), transaction_date=date(2026, 6, 1)),
        ]
        result = normalize_pdf_statement_rows_batch(rows)
        assert result.accepted_rows[0].merchant_raw == "A"
        assert result.accepted_rows[1].merchant_raw == "B"
        assert result.accepted_rows[2].merchant_raw == "C"

    def test_blocked_rows_preserve_input_order(self) -> None:
        rows = [
            _make_row(description="Blocked A", amount=None, transaction_date=date(2026, 6, 1)),
            _make_row(description="Blocked B", amount=None, transaction_date=date(2026, 6, 1)),
        ]
        result = normalize_pdf_statement_rows_batch(rows)
        assert result.blocked_rows[0].description == "Blocked A"
        assert result.blocked_rows[1].description == "Blocked B"

    def test_empty_input_produces_empty_result(self) -> None:
        result = normalize_pdf_statement_rows_batch([])
        assert result.total_rows == 0
        assert result.accepted_count == 0
        assert result.blocked_count == 0
        assert result.has_blocked_rows is False
        assert len(result.accepted_rows) == 0
        assert len(result.blocked_rows) == 0
        assert result.blocked_reason_counts == ()

    def test_blocked_reason_counts_single_row(self) -> None:
        row = _make_row(
            amount=None,
            currency="",
            description="",
            amount_direction=StatementAmountDirection.UNKNOWN,
        )
        result = normalize_pdf_statement_rows_batch([row])
        counts = result.blocked_reason_counts
        assert len(counts) >= 3
        assert (PdfBlockedReason.MISSING_AMOUNT, 1) in counts
        assert (PdfBlockedReason.MISSING_CURRENCY, 1) in counts
        assert (PdfBlockedReason.UNKNOWN_DIRECTION, 1) in counts

    def test_blocked_reason_counts_aggregates(self) -> None:
        rows = [
            _make_row(amount=None, transaction_date=date(2026, 6, 1)),
            _make_row(amount=None, transaction_date=date(2026, 6, 1)),
            _make_row(currency="", amount=Decimal("10.00")),
        ]
        result = normalize_pdf_statement_rows_batch(rows)
        counts = result.blocked_reason_counts
        assert (PdfBlockedReason.MISSING_AMOUNT, 2) in counts
        assert (PdfBlockedReason.MISSING_CURRENCY, 1) in counts

    def test_blocked_reason_counts_deterministic(self) -> None:
        rows = [
            _make_row(amount=None, transaction_date=date(2026, 6, 1)),
            _make_row(currency="", amount=Decimal("10.00")),
        ]
        r1 = normalize_pdf_statement_rows_batch(rows)
        r2 = normalize_pdf_statement_rows_batch(rows)
        assert r1.blocked_reason_counts == r2.blocked_reason_counts

    def test_blocked_reason_counts_sorted_by_reason_value(self) -> None:
        rows = [
            _make_row(
                amount=None,
                currency="",
                description="",
                amount_direction=StatementAmountDirection.UNKNOWN,
            ),
        ]
        result = normalize_pdf_statement_rows_batch(rows)
        counts = result.blocked_reason_counts
        reason_values = [r.value for r, _ in counts]
        assert reason_values == sorted(reason_values)

    def test_blocked_row_fingerprint_is_set(self) -> None:
        row = _make_row(amount=None, transaction_date=date(2026, 6, 1))
        result = normalize_pdf_statement_rows_batch([row])
        assert result.blocked_rows[0].row_fingerprint != ""
        assert len(result.blocked_rows[0].row_fingerprint) == 64
        int(result.blocked_rows[0].row_fingerprint, 16)

    def test_blocked_row_source_fields_preserved(self) -> None:
        row = _make_row(
            source_statement_id="stmt-abc",
            attachment_path="/tmp/stmt.pdf",
            source_page_number=3,
            source_row_ref="r-7",
            description="Some Merchant",
            amount=None,
            transaction_date=date(2026, 6, 1),
            raw_row_text="raw text here",
        )
        result = normalize_pdf_statement_rows_batch([row])
        blocked = result.blocked_rows[0]
        assert blocked.source_statement_id == "stmt-abc"
        assert blocked.attachment_path == "/tmp/stmt.pdf"
        assert blocked.source_page_number == 3
        assert blocked.source_row_ref == "r-7"
        assert blocked.description == "Some Merchant"
        assert blocked.raw_row_text == "raw text here"

    def test_blocked_row_blocked_reasons_preserved(self) -> None:
        row = _make_row(amount=None, transaction_date=date(2026, 6, 1))
        result = normalize_pdf_statement_rows_batch([row])
        assert PdfBlockedReason.MISSING_AMOUNT in result.blocked_rows[0].blocked_reasons

    def test_generator_input_works(self) -> None:
        gen = (
            _make_row(
                amount=Decimal(str(i) + ".00"),
                transaction_date=date(2026, 6, 1),
            )
            for i in range(1, 4)
        )
        result = normalize_pdf_statement_rows_batch(gen)
        assert result.accepted_count == 3

    def test_result_is_frozen(self) -> None:
        rows = [_make_row(amount=Decimal("10.00"), transaction_date=date(2026, 6, 1))]
        result = normalize_pdf_statement_rows_batch(rows)
        with pytest.raises(Exception):
            result.total_rows = 99  # type: ignore[misc]

    def test_blocked_row_dataclass_is_frozen(self) -> None:
        br = PdfBlockedStatementRow(
            source_statement_id="s",
            attachment_path="/tmp/x.pdf",
            source_page_number=None,
            source_row_ref=None,
            description="desc",
            blocked_reasons=(PdfBlockedReason.MISSING_AMOUNT,),
            raw_row_text="raw",
            row_fingerprint="fp",
        )
        with pytest.raises(Exception):
            br.description = "changed"  # type: ignore[misc]

    def test_single_valid_row(self) -> None:
        row = _make_row(
            transaction_date=date(2026, 6, 28),
            amount=Decimal("42.50"),
            currency="SGD",
            description="Valid",
        )
        result = normalize_pdf_statement_rows_batch([row])
        assert result.accepted_count == 1
        assert result.blocked_count == 0
        assert result.accepted_rows[0].merchant_raw == "Valid"

    def test_single_blocked_row(self) -> None:
        row = _make_row(amount=None, transaction_date=date(2026, 6, 1))
        result = normalize_pdf_statement_rows_batch([row])
        assert result.blocked_count == 1
        assert result.accepted_count == 0
        assert result.has_blocked_rows is True

    def test_blocked_row_fingerprint_fallback(self) -> None:
        """Blocked rows retain a deterministic full SHA-256 fingerprint."""
        row = _make_row(amount=None, transaction_date=date(2026, 6, 1))
        result = normalize_pdf_statement_rows_batch([row])
        fp = result.blocked_rows[0].row_fingerprint
        assert len(fp) == 64
        int(fp, 16)

    def test_blocked_reason_counts_is_tuple_of_tuples(self) -> None:
        rows = [
            _make_row(amount=None, transaction_date=date(2026, 6, 1)),
            _make_row(currency="", amount=Decimal("10.00")),
        ]
        result = normalize_pdf_statement_rows_batch(rows)
        assert isinstance(result.blocked_reason_counts, tuple)
        for item in result.blocked_reason_counts:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], PdfBlockedReason)
            assert isinstance(item[1], int)

    def test_has_blocked_rows_false_when_all_valid(self) -> None:
        rows = [
            _make_row(amount=Decimal("1.00"), transaction_date=date(2026, 6, 1)),
        ]
        result = normalize_pdf_statement_rows_batch(rows)
        assert result.has_blocked_rows is False

    def test_has_blocked_rows_true_when_any_blocked(self) -> None:
        rows = [
            _make_row(amount=Decimal("1.00"), transaction_date=date(2026, 6, 1)),
            _make_row(amount=None, transaction_date=date(2026, 6, 1)),
        ]
        result = normalize_pdf_statement_rows_batch(rows)
        assert result.has_blocked_rows is True

    def test_batch_normalization_is_read_only(self) -> None:
        """Batch normalization must not touch database or file system."""
        import inspect

        source = inspect.getsource(normalize_pdf_statement_rows_batch)
        assert "sqlite3" not in source.lower()
        assert "finance.db" not in source
        assert "open(" not in source

    def test_accepted_rows_are_structured_statement_rows(self) -> None:
        rows = [
            _make_row(amount=Decimal("1.00"), transaction_date=date(2026, 6, 1)),
        ]
        result = normalize_pdf_statement_rows_batch(rows)
        for row in result.accepted_rows:
            assert isinstance(row, StructuredStatementRow)

    def test_batch_preserves_normalized_row_semantics(self) -> None:
        """A valid row normalized through batch must produce the same result
        as the single-row checked normalizer."""
        row = _make_row(
            transaction_date=date(2026, 6, 28),
            amount=Decimal("42.50"),
            currency="SGD",
            description="GrabFood",
        )
        batch_result = normalize_pdf_statement_rows_batch([row])
        single_result = normalize_pdf_statement_row_checked(row)
        assert batch_result.accepted_rows[0].amount == single_result.amount
        assert batch_result.accepted_rows[0].currency == single_result.currency
        assert batch_result.accepted_rows[0].row_fingerprint == single_result.row_fingerprint
