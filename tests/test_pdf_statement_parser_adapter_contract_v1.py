"""Tests for PDF Statement Parser Adapter Contract v1.

Verifies that the adapter boundary is deterministic, read-only, correctly
maps synthetic parser payloads into ParsedPdfStatementRow objects,
preserves source evidence, and integrates with the existing bridge
normalizer and import review fixture. Uses synthetic data only — no real
PDFs, no OCR, no live DB.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import ParsedPdfStatementRow
from finance_core.reconciliation.pdf_statement_parser_adapter_contract import (
    PdfParserAdapterBlockedReason,
    PdfParserAdapterResult,
    PdfParserRowPayload,
    PdfParserSmokeTestResult,
    PdfParserStatementPayload,
    _adapt_single_row,
    _infer_direction,
    _normalise_currency,
    _parse_amount,
    _parse_date,
    _resolve_description,
    _RowAdaptResult,
    adapt_pdf_parser_payload_to_statement_rows,
    run_smoke_test,
)

# ---------------------------------------------------------------------------
# Synthetic parser payload helpers
# ---------------------------------------------------------------------------


def _make_row_payload(
    *,
    source_page_number: int | None = 1,
    source_row_ref: str | None = "page-1:line-1",
    source_row_number: int | None = 1,
    transaction_date_raw: str | None = "2026-06-28",
    posted_date_raw: str | None = "2026-06-29",
    description: str = "Test Merchant",
    merchant_raw: str = "",
    amount_raw: str | None = "100.00",
    currency_raw: str = "SGD",
    debit_credit_raw: str | None = "D",
    raw_row_text: str = "28/06 Test Merchant  100.00",
) -> PdfParserRowPayload:
    return PdfParserRowPayload(
        source_page_number=source_page_number,
        source_row_ref=source_row_ref,
        transaction_date_raw=transaction_date_raw,
        posted_date_raw=posted_date_raw,
        description=description,
        merchant_raw=merchant_raw,
        amount_raw=amount_raw,
        currency_raw=currency_raw,
        debit_credit_raw=debit_credit_raw,
        raw_row_text=raw_row_text,
        source_row_number=source_row_number,
        source_text_excerpt=raw_row_text,
    )


def _make_statement_payload(
    *,
    source_statement_id: str = "stmt-001",
    attachment_path: str = "/tmp/fake.pdf",
    rows: tuple[PdfParserRowPayload, ...] | None = None,
) -> PdfParserStatementPayload:
    if rows is None:
        rows = (_make_row_payload(),)
    return PdfParserStatementPayload(
        source_statement_id=source_statement_id,
        attachment_path=attachment_path,
        rows=rows,
        source_content_hash="a" * 64,
        source_filename="fake.pdf",
    )


# ===================================================================
# 1. Empty parser payload
# ===================================================================


class TestEmptyParserPayload:
    """Empty parser payload must produce empty adapter result."""

    def test_empty_payload(self) -> None:
        payload = _make_statement_payload(rows=())
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert isinstance(result, PdfParserAdapterResult)
        assert result.total_rows == 0
        assert result.adapted_count == 0
        assert result.blocked_count == 0
        assert result.adapted_rows == ()
        assert result.blocked_row_indices == ()


# ===================================================================
# 2. Single valid parser row
# ===================================================================


class TestSingleValidParserRow:
    """A single well-formed parser row must adapt successfully."""

    def test_single_valid_row(self) -> None:
        payload = _make_statement_payload()
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.total_rows == 1
        assert result.adapted_count == 1
        assert result.blocked_count == 0
        assert result.blocked_row_indices == ()
        assert len(result.adapted_rows) == 1

    def test_adapted_row_is_parsed_pdf_statement_row(self) -> None:
        payload = _make_statement_payload()
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        row = result.adapted_rows[0]
        assert isinstance(row, ParsedPdfStatementRow)

    def test_adapted_row_fields_mapped_correctly(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(
                    source_page_number=3,
                    source_row_ref="p3r5",
                    transaction_date_raw="2026-06-28",
                    posted_date_raw="2026-06-29",
                    description="Coffee Shop",
                    amount_raw="5.50",
                    currency_raw="MYR",
                    debit_credit_raw="D",
                    raw_row_text="28/06 Coffee Shop  5.50",
                ),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        row = result.adapted_rows[0]
        assert row.source_statement_id == "stmt-001"
        assert row.attachment_path == "/tmp/fake.pdf"
        assert row.source_page_number == 3
        assert row.source_row_ref == "p3r5"
        assert row.transaction_date == date(2026, 6, 28)
        assert row.posted_date == date(2026, 6, 29)
        assert row.description == "Coffee Shop"
        assert row.amount == Decimal("5.50")
        assert row.currency == "MYR"
        assert row.amount_direction == StatementAmountDirection.DEBIT
        assert row.raw_row_text == "28/06 Coffee Shop  5.50"

    def test_merchant_raw_priority_over_description(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(
                    description="Original Desc",
                    merchant_raw="Starbucks",
                ),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_rows[0].description == "Starbucks"

    def test_description_fallback_when_merchant_empty(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(
                    description="Fallback Desc",
                    merchant_raw="",
                ),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_rows[0].description == "Fallback Desc"

    def test_credit_indicator_mapped_to_credit(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(
                    debit_credit_raw="CR",
                    amount_raw="500.00",
                ),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_rows[0].amount_direction == StatementAmountDirection.CREDIT

    def test_negative_amount_without_direction_requires_review(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(
                    debit_credit_raw=None,
                    amount_raw="-200.00",
                ),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_rows[0].amount_direction == StatementAmountDirection.UNKNOWN


# ===================================================================
# 3. Multiple valid parser rows
# ===================================================================


class TestMultipleValidParserRows:
    """Multiple well-formed parser rows must all adapt successfully."""

    def test_multiple_valid_rows(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(description="Row 1", amount_raw="10.00"),
                _make_row_payload(description="Row 2", amount_raw="20.00"),
                _make_row_payload(description="Row 3", amount_raw="30.00"),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.total_rows == 3
        assert result.adapted_count == 3
        assert result.blocked_count == 0
        assert len(result.adapted_rows) == 3
        assert result.adapted_rows[0].description == "Row 1"
        assert result.adapted_rows[1].description == "Row 2"
        assert result.adapted_rows[2].description == "Row 3"

    def test_ordering_preserved(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(description="A"),
                _make_row_payload(description="B"),
                _make_row_payload(description="C"),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert [r.description for r in result.adapted_rows] == ["A", "B", "C"]


# ===================================================================
# 4. Blocked row — unparseable amount
# ===================================================================


class TestBlockedRowUnparseableAmount:
    """Rows with unparseable amounts must be blocked at the adapter level."""

    def test_unparseable_amount_blocked(self) -> None:
        payload = _make_statement_payload(
            rows=(_make_row_payload(amount_raw="not-a-number"),),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.total_rows == 1
        assert result.adapted_count == 0
        assert result.blocked_count == 1
        assert result.blocked_row_indices == (0,)

    def test_unparseable_amount_with_valid_rows(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(description="Valid", amount_raw="100.00"),
                _make_row_payload(description="Bad Amount", amount_raw="???"),
                _make_row_payload(description="Also Valid", amount_raw="200.00"),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.total_rows == 3
        assert result.adapted_count == 2
        assert result.blocked_count == 1
        assert result.blocked_row_indices == (1,)
        assert result.adapted_rows[0].description == "Valid"
        assert result.adapted_rows[1].description == "Also Valid"


# ===================================================================
# 5. Source evidence preservation
# ===================================================================


class TestSourceEvidencePreservation:
    """Source evidence must be preserved through the adapter."""

    def test_attachment_path_preserved(self) -> None:
        payload = _make_statement_payload(attachment_path="/docs/stmt.pdf")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_rows[0].attachment_path == "/docs/stmt.pdf"

    def test_source_page_number_preserved(self) -> None:
        payload = _make_statement_payload(rows=(_make_row_payload(source_page_number=7),))
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_rows[0].source_page_number == 7

    def test_source_row_ref_preserved(self) -> None:
        payload = _make_statement_payload(rows=(_make_row_payload(source_row_ref="r-42"),))
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_rows[0].source_row_ref == "r-42"

    def test_source_statement_id_preserved(self) -> None:
        payload = _make_statement_payload(source_statement_id="stmt-abc-123")
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_rows[0].source_statement_id == "stmt-abc-123"


# ===================================================================
# 6. Attachment path is not read
# ===================================================================


class TestAttachmentPathNotRead:
    """Adapter must not open or read the attachment path."""

    def test_no_file_io_in_module(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_parser_adapter_contract as pspac

        source = inspect.getsource(pspac)
        assert "open(" not in source
        assert "Path(" not in source
        assert "os.path" not in source

    def test_nonexistent_path_does_not_crash(self) -> None:
        payload = _make_statement_payload(
            attachment_path="/nonexistent/path/to/file.pdf",
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_count == 1
        assert result.adapted_rows[0].attachment_path == "/nonexistent/path/to/file.pdf"


# ===================================================================
# 7. Raw row text preserved for audit path
# ===================================================================


class TestRawRowTextPreservation:
    """raw_row_text must be preserved through the adapter."""

    def test_raw_row_text_preserved(self) -> None:
        payload = _make_statement_payload(
            rows=(_make_row_payload(raw_row_text="01/06 STARBUCKS COFFEE  18.50"),),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_rows[0].raw_row_text == "01/06 STARBUCKS COFFEE  18.50"

    def test_raw_row_text_empty_string_preserved(self) -> None:
        payload = _make_statement_payload(rows=(_make_row_payload(raw_row_text=""),))
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_rows[0].raw_row_text == ""


# ===================================================================
# 8. Deterministic ordering
# ===================================================================


class TestDeterministicOrdering:
    """Adapter must produce deterministic results for the same input."""

    def test_same_input_same_output(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(description="A", amount_raw="10.00"),
                _make_row_payload(description="B", amount_raw="20.00"),
            ),
        )
        r1 = adapt_pdf_parser_payload_to_statement_rows(payload)
        r2 = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert r1.total_rows == r2.total_rows
        assert r1.adapted_count == r2.adapted_count
        assert r1.blocked_count == r2.blocked_count
        assert r1.blocked_row_indices == r2.blocked_row_indices
        for a, b in zip(r1.adapted_rows, r2.adapted_rows):
            assert a.description == b.description
            assert a.amount == b.amount

    def test_ordering_is_input_order(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(description="Third", amount_raw="30.00"),
                _make_row_payload(description="First", amount_raw="10.00"),
                _make_row_payload(description="Second", amount_raw="20.00"),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert [r.description for r in result.adapted_rows] == ["Third", "First", "Second"]


# ===================================================================
# 9. No mutation of input payload
# ===================================================================


class TestNoMutationOfInputPayload:
    """The adapter must not mutate the input payload."""

    def test_input_payload_unchanged(self) -> None:
        row = _make_row_payload(description="Original")
        payload = _make_statement_payload(rows=(row,))
        original_desc = payload.rows[0].description
        adapt_pdf_parser_payload_to_statement_rows(payload)
        assert payload.rows[0].description == original_desc

    def test_input_rows_unchanged(self) -> None:
        rows = (
            _make_row_payload(description="A", amount_raw="10.00"),
            _make_row_payload(description="B", amount_raw="20.00"),
        )
        payload = PdfParserStatementPayload(
            source_statement_id="stmt-001",
            attachment_path="/tmp/fake.pdf",
            rows=rows,
        )
        adapt_pdf_parser_payload_to_statement_rows(payload)
        assert payload.rows[0].description == "A"
        assert payload.rows[0].amount_raw == "10.00"
        assert payload.rows[1].description == "B"
        assert payload.rows[1].amount_raw == "20.00"


# ===================================================================
# 10. Adapter result can be passed into normalize_pdf_statement_rows_batch
# ===================================================================


class TestIntegrationWithBatchNormalizer:
    """Adapted rows must be compatible with normalize_pdf_statement_rows_batch."""

    def test_adapter_result_feeds_batch_normalizer(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(
                    description="Coffee",
                    amount_raw="5.50",
                    currency_raw="MYR",
                    transaction_date_raw="2026-07-01",
                    debit_credit_raw="D",
                ),
                _make_row_payload(
                    description="Refund",
                    amount_raw="20.00",
                    currency_raw="MYR",
                    transaction_date_raw="2026-07-02",
                    debit_credit_raw="CR",
                ),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        from finance_core.reconciliation.pdf_statement_bridge import (
            normalize_pdf_statement_rows_batch,
        )

        batch = normalize_pdf_statement_rows_batch(result.adapted_rows)
        assert batch.total_rows == 2
        assert batch.accepted_count == 2
        assert batch.blocked_count == 0

    def test_batch_normalizer_blocks_invalid_adapted_rows(self) -> None:
        """Rows adapted by the adapter with missing critical fields will
        be blocked by the batch normalizer."""
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(
                    description="No Currency Row",
                    amount_raw="10.00",
                    currency_raw="",
                    transaction_date_raw="2026-07-01",
                ),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        from finance_core.reconciliation.pdf_statement_bridge import (
            normalize_pdf_statement_rows_batch,
        )

        batch = normalize_pdf_statement_rows_batch(result.adapted_rows)
        assert batch.total_rows == 1
        assert batch.accepted_count == 0
        assert batch.blocked_count == 1


# ===================================================================
# 11. Full chain smoke test through import review fixture
# ===================================================================


class TestFullChainSmokeTest:
    """run_smoke_test must exercise the full pipeline."""

    def test_smoke_test_runs_end_to_end(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(
                    description="Coffee Shop",
                    amount_raw="5.50",
                    currency_raw="MYR",
                    transaction_date_raw="2026-07-03",
                    posted_date_raw="2026-07-04",
                    source_page_number=1,
                    source_row_ref="r1",
                    debit_credit_raw="D",
                ),
                _make_row_payload(
                    description="Salary",
                    amount_raw="8000.00",
                    currency_raw="MYR",
                    transaction_date_raw="2026-06-28",
                    debit_credit_raw="CR",
                ),
            ),
        )
        smoke = run_smoke_test(payload)
        assert isinstance(smoke, PdfParserSmokeTestResult)
        assert smoke.adapter_result.adapted_count == 2
        assert smoke.batch_result.accepted_count == 2
        assert len(smoke.review_fixture.accepted_rows) == 2
        assert smoke.dashboard_payload is not None
        assert smoke.audit_payload is not None

    def test_smoke_test_with_blocked_adapter_row(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(description="Valid", amount_raw="10.00"),
                _make_row_payload(description="Bad", amount_raw="???"),
                _make_row_payload(description="Also Valid", amount_raw="20.00"),
            ),
        )
        smoke = run_smoke_test(payload)
        assert smoke.adapter_result.adapted_count == 2
        assert smoke.adapter_result.blocked_count == 1
        assert smoke.batch_result.accepted_count == 2

    def test_smoke_test_deterministic(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(description="A", amount_raw="10.00"),
                _make_row_payload(description="B", amount_raw="20.00"),
            ),
        )
        s1 = run_smoke_test(payload)
        s2 = run_smoke_test(payload)
        assert s1.dashboard_payload == s2.dashboard_payload
        assert s1.audit_payload == s2.audit_payload


# ===================================================================
# 12. Dashboard payload excludes sensitive source evidence
# ===================================================================


class TestDashboardPayloadSafety:
    """Dashboard payload must not expose sensitive source evidence."""

    def test_dashboard_payload_excludes_attachment_path(self) -> None:
        payload = _make_statement_payload(
            attachment_path="/sensitive/path.pdf",
            rows=(_make_row_payload(),),
        )
        smoke = run_smoke_test(payload)
        dash_str = json.dumps(smoke.dashboard_payload, default=str)
        assert "attachment_path" not in dash_str
        assert "/sensitive/path.pdf" not in dash_str

    def test_dashboard_payload_excludes_raw_row_text(self) -> None:
        payload = _make_statement_payload(
            rows=(_make_row_payload(raw_row_text="secret data"),),
        )
        smoke = run_smoke_test(payload)
        dash_str = json.dumps(smoke.dashboard_payload, default=str)
        assert "raw_row_text" not in dash_str

    def test_dashboard_payload_excludes_raw_row_payload(self) -> None:
        payload = _make_statement_payload(
            rows=(_make_row_payload(),),
        )
        smoke = run_smoke_test(payload)
        dash_str = json.dumps(smoke.dashboard_payload, default=str)
        assert "raw_row_payload" not in dash_str


# ===================================================================
# 13. Audit payload includes source evidence where available
# ===================================================================


class TestAuditPayloadIncludesSourceEvidence:
    """Audit payload must include source evidence for traceability."""

    def test_audit_payload_includes_attachment_path(self) -> None:
        payload = _make_statement_payload(
            attachment_path="/audit/stmt.pdf",
            rows=(_make_row_payload(),),
        )
        smoke = run_smoke_test(payload)
        assert (
            smoke.audit_payload["accepted"][0]["raw_row_payload"]["attachment_path"]
            == "/audit/stmt.pdf"
        )

    def test_audit_payload_includes_raw_row_payload(self) -> None:
        payload = _make_statement_payload(
            rows=(_make_row_payload(),),
        )
        smoke = run_smoke_test(payload)
        assert "raw_row_payload" in smoke.audit_payload["accepted"][0]


# ===================================================================
# 14. No DB access
# ===================================================================


class TestNoDbAccess:
    """Module must not reference database/finance.db or sqlite3."""

    def test_no_live_db_path_in_module(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_parser_adapter_contract as pspac

        source = inspect.getsource(pspac)
        assert "finance.db" not in source

    def test_no_sqlite3_import(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_parser_adapter_contract as pspac

        source = inspect.getsource(pspac)
        assert "sqlite3" not in source


# ===================================================================
# 15. No OCR/PDF dependency
# ===================================================================


class TestNoOcrPdfDependency:
    """Module must not reference PDF/OCR libraries."""

    def test_no_pdf_ocr_imports(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_parser_adapter_contract as pspac

        source = inspect.getsource(pspac)
        # Strip the module docstring so non-goal text does not count
        source_no_docstring = (
            source.partition('"""')[2].partition('"""')[2] if '"""' in source else source
        )
        source_lower = source_no_docstring.lower()
        for dep in (
            "import pdfplumber",
            "import pytesseract",
            "import pypdf",
            "import camelot",
            "import tabula",
            "from pdfplumber",
            "from pytesseract",
            "from pypdf",
            "from camelot",
            "from tabula",
        ):
            assert dep not in source_lower, f"Unexpected dependency: {dep}"
        assert "import ocr" not in source_lower
        assert "from ocr" not in source_lower


# ===================================================================
# 16. Immutability
# ===================================================================


class TestImmutability:
    """All exported dataclasses must be frozen."""

    def test_parser_row_payload_is_frozen(self) -> None:
        row = _make_row_payload()
        with pytest.raises(Exception):
            row.description = "changed"  # type: ignore[misc]

    def test_parser_statement_payload_is_frozen(self) -> None:
        payload = _make_statement_payload()
        with pytest.raises(Exception):
            payload.source_statement_id = "changed"  # type: ignore[misc]

    def test_adapter_result_is_frozen(self) -> None:
        payload = _make_statement_payload()
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        with pytest.raises(Exception):
            result.total_rows = 99  # type: ignore[misc]

    def test_smoke_test_result_is_frozen(self) -> None:
        payload = _make_statement_payload()
        smoke = run_smoke_test(payload)
        with pytest.raises(Exception):
            smoke.adapter_result = smoke.adapter_result  # type: ignore[misc]


# ===================================================================
# 17. Date parsing
# ===================================================================


class TestDateParsing:
    """Date parsing must handle common formats."""

    def test_iso_date(self) -> None:
        result = _parse_date("2026-06-28")
        assert result == date(2026, 6, 28)

    def test_dmy_slash_date(self) -> None:
        result = _parse_date("28/06/2026")
        assert result == date(2026, 6, 28)

    def test_dmy_dash_date(self) -> None:
        result = _parse_date("28-06-2026")
        assert result == date(2026, 6, 28)

    def test_dd_mon_yyyy_date(self) -> None:
        result = _parse_date("28 Jun 2026")
        assert result == date(2026, 6, 28)

    def test_none_date_returns_none(self) -> None:
        assert _parse_date(None) is None

    def test_empty_date_returns_none(self) -> None:
        assert _parse_date("") is None

    def test_whitespace_date_returns_none(self) -> None:
        assert _parse_date("   ") is None

    def test_unparseable_date_returns_none(self) -> None:
        assert _parse_date("not-a-date") is None


# ===================================================================
# 18. Amount parsing
# ===================================================================


class TestAmountParsing:
    """Amount parsing must handle common formats."""

    def test_simple_amount(self) -> None:
        assert _parse_amount("100.00") == Decimal("100.00")

    def test_negative_amount(self) -> None:
        assert _parse_amount("-50.00") == Decimal("-50.00")

    def test_parentheses_negative(self) -> None:
        assert _parse_amount("(50.00)") == Decimal("-50.00")

    def test_comma_thousands(self) -> None:
        assert _parse_amount("1,234.56") == Decimal("1234.56")

    def test_currency_symbol(self) -> None:
        assert _parse_amount("$100.00") == Decimal("100.00")

    def test_none_amount_returns_none(self) -> None:
        assert _parse_amount(None) is None

    def test_empty_amount_returns_none(self) -> None:
        assert _parse_amount("") is None

    def test_whitespace_amount_returns_none(self) -> None:
        assert _parse_amount("   ") is None

    def test_unparseable_amount_returns_none(self) -> None:
        assert _parse_amount("abc") is None


# ===================================================================
# 19. Direction inference
# ===================================================================


class TestDirectionInference:
    """Direction inference must map raw indicators correctly."""

    def test_debit_d(self) -> None:
        assert _infer_direction("D", Decimal("100.00")) == StatementAmountDirection.DEBIT

    def test_debit_dr(self) -> None:
        assert _infer_direction("DR", Decimal("100.00")) == StatementAmountDirection.DEBIT

    def test_credit_c(self) -> None:
        assert _infer_direction("C", Decimal("100.00")) == StatementAmountDirection.CREDIT

    def test_credit_cr(self) -> None:
        assert _infer_direction("CR", Decimal("100.00")) == StatementAmountDirection.CREDIT

    def test_negative_amount_does_not_infer_credit(self) -> None:
        assert _infer_direction(None, Decimal("-100.00")) == StatementAmountDirection.UNKNOWN

    def test_positive_amount_does_not_infer_debit(self) -> None:
        assert _infer_direction(None, Decimal("100.00")) == StatementAmountDirection.UNKNOWN

    def test_unrecognised_indicator_returns_unknown(self) -> None:
        assert _infer_direction("XYZ", Decimal("100.00")) == StatementAmountDirection.UNKNOWN

    def test_none_indicator_none_amount_returns_unknown(self) -> None:
        assert _infer_direction(None, None) == StatementAmountDirection.UNKNOWN

    def test_case_insensitive_indicator(self) -> None:
        assert _infer_direction("dr", Decimal("100.00")) == StatementAmountDirection.DEBIT
        assert _infer_direction("cr", Decimal("100.00")) == StatementAmountDirection.CREDIT


# ===================================================================
# 20. Currency normalisation
# ===================================================================


class TestCurrencyNormalisation:
    """Currency must be normalised to uppercase stripped form."""

    def test_uppercase(self) -> None:
        assert _normalise_currency("sgd") == "SGD"

    def test_strip_whitespace(self) -> None:
        assert _normalise_currency("  MYR  ") == "MYR"

    def test_empty_returns_empty(self) -> None:
        assert _normalise_currency("") == ""

    def test_already_normalised(self) -> None:
        assert _normalise_currency("USD") == "USD"


# ===================================================================
# 21. Description resolution
# ===================================================================


class TestDescriptionResolution:
    """Description must resolve from merchant_raw or description."""

    def test_merchant_raw_priority(self) -> None:
        assert _resolve_description("desc", "Starbucks") == "Starbucks"

    def test_description_fallback(self) -> None:
        assert _resolve_description("Fallback", "") == "Fallback"

    def test_whitespace_merchant_falls_back(self) -> None:
        assert _resolve_description("Fallback", "   ") == "Fallback"


# ===================================================================
# 22. PdfParserAdapterResult helper properties
# ===================================================================


class TestAdapterResultProperties:
    """PdfParserAdapterResult must carry correct counts."""

    def test_counts_consistent(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(description="A", amount_raw="10.00"),
                _make_row_payload(description="B", amount_raw="???"),
                _make_row_payload(description="C", amount_raw="30.00"),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_count + result.blocked_count == result.total_rows
        assert result.adapted_count == len(result.adapted_rows)
        assert result.blocked_count == len(result.blocked_row_indices)


# ===================================================================
# 23. _adapt_single_row internal function
# ===================================================================


class TestAdaptSingleRow:
    """Internal _adapt_single_row must handle various scenarios."""

    def test_valid_row(self) -> None:
        parser_row = _make_row_payload()
        result = _adapt_single_row("stmt-001", "/tmp/fake.pdf", parser_row)
        assert result.is_blocked is False
        assert result.row is not None
        assert result.row.description == "Test Merchant"

    def test_unparseable_amount(self) -> None:
        parser_row = _make_row_payload(amount_raw="???")
        result = _adapt_single_row("stmt-001", "/tmp/fake.pdf", parser_row)
        assert result.is_blocked is True
        assert result.row is None
        assert result.blocked_reason == PdfParserAdapterBlockedReason.UNPARSEABLE_AMOUNT


# ===================================================================
# 24. _RowAdaptResult immutability
# ===================================================================


class TestRowAdaptResult:
    """_RowAdaptResult must be frozen."""

    def test_row_adapt_result_is_frozen(self) -> None:
        result = _RowAdaptResult(row=None, is_blocked=True, blocked_reason="test")
        with pytest.raises(Exception):
            result.is_blocked = False  # type: ignore[misc]


# ===================================================================
# 25. Adapter Reason Codes constants
# ===================================================================


class TestAdapterBlockedReasonCodes:
    """Adapter blocked reason codes must be defined and stable."""

    def test_reason_codes_exist(self) -> None:
        assert PdfParserAdapterBlockedReason.UNPARSEABLE_AMOUNT == "unparseable_amount"
        assert PdfParserAdapterBlockedReason.UNPARSEABLE_DATE == "unparseable_date"
        assert PdfParserAdapterBlockedReason.UNRECOGNISED_DIRECTION == "unrecognised_direction"
        assert PdfParserAdapterBlockedReason.MISSING_REQUIRED_FIELD == "missing_required_field"


# ===================================================================
# 26. Amount normalisation (absolute value)
# ===================================================================


class TestAmountNormalisation:
    """Parsed amounts must be stored as absolute values (direction separate)."""

    def test_negative_amount_is_absolute_in_row(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(
                    amount_raw="-100.00",
                    debit_credit_raw=None,
                ),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_rows[0].amount == Decimal("100.00")
        assert result.adapted_rows[0].amount_direction == StatementAmountDirection.UNKNOWN

    def test_parentheses_amount_is_absolute_in_row(self) -> None:
        payload = _make_statement_payload(
            rows=(
                _make_row_payload(
                    amount_raw="(200.00)",
                    debit_credit_raw=None,
                ),
            ),
        )
        result = adapt_pdf_parser_payload_to_statement_rows(payload)
        assert result.adapted_rows[0].amount == Decimal("200.00")
        assert result.adapted_rows[0].amount_direction == StatementAmountDirection.UNKNOWN


# ===================================================================
# 27. Full pipeline: adapter -> batch -> review -> dashboard + audit
# ===================================================================


class TestFullPipeline:
    """End-to-end pipeline from adapter through review fixture."""

    def test_full_pipeline_flow(self) -> None:
        payload = _make_statement_payload(
            source_statement_id="stmt-e2e",
            attachment_path="/tmp/stmt-e2e.pdf",
            rows=(
                _make_row_payload(
                    source_page_number=1,
                    source_row_ref="p1r1",
                    transaction_date_raw="2026-07-01",
                    description="Coffee Shop",
                    amount_raw="5.50",
                    currency_raw="MYR",
                    debit_credit_raw="D",
                    raw_row_text="01/07 Coffee Shop  5.50",
                ),
                _make_row_payload(
                    source_page_number=1,
                    source_row_ref="p1r2",
                    transaction_date_raw="2026-07-02",
                    description="Unknown",
                    amount_raw="???",
                    currency_raw="MYR",
                    raw_row_text="02/07 Unknown  ???",
                ),
                _make_row_payload(
                    source_page_number=2,
                    source_row_ref="p2r1",
                    transaction_date_raw="2026-06-28",
                    description="Salary",
                    amount_raw="8000.00",
                    currency_raw="MYR",
                    debit_credit_raw="CR",
                    raw_row_text="28/06 Salary  8000.00 CR",
                ),
            ),
        )
        smoke = run_smoke_test(payload)

        # Adapter
        assert smoke.adapter_result.total_rows == 3
        assert smoke.adapter_result.adapted_count == 2
        assert smoke.adapter_result.blocked_count == 1
        assert smoke.adapter_result.blocked_row_indices == (1,)

        # Batch normalizer
        assert smoke.batch_result.total_rows == 2
        assert smoke.batch_result.accepted_count == 2
        assert smoke.batch_result.blocked_count == 0

        # Review fixture
        assert len(smoke.review_fixture.accepted_rows) == 2
        assert smoke.review_fixture.accepted_rows[0].merchant_raw == "Coffee Shop"
        assert smoke.review_fixture.accepted_rows[1].merchant_raw == "Salary"

        # Dashboard payload
        assert smoke.dashboard_payload["summary"]["total_rows"] == 2
        assert smoke.dashboard_payload["summary"]["accepted_count"] == 2

        # Audit payload
        assert smoke.audit_payload["summary"]["total_rows"] == 2
        assert len(smoke.audit_payload["accepted"]) == 2


# ===================================================================
# 28. Public exports
# ===================================================================


class TestPublicExports:
    """All public names must be exported via __all__."""

    def test_public_names_in_all(self) -> None:
        from finance_core.reconciliation import pdf_statement_parser_adapter_contract as pspac

        assert hasattr(pspac, "PdfParserRowPayload")
        assert hasattr(pspac, "PdfParserStatementPayload")
        assert hasattr(pspac, "PdfParserAdapterResult")
        assert hasattr(pspac, "PdfParserAdapterBlockedReason")
        assert hasattr(pspac, "PdfParserSmokeTestResult")
        assert hasattr(pspac, "adapt_pdf_parser_payload_to_statement_rows")
        assert hasattr(pspac, "run_smoke_test")
