"""Tests for PDF Statement Parser Template Adapter v1."""

from __future__ import annotations

from datetime import date as Date
from decimal import Decimal

import pytest

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import (
    ParsedPdfStatementRow,
    build_pdf_statement_row_fingerprint,
    normalize_pdf_statement_rows_batch,
    validate_pdf_statement_row,
)
from finance_core.reconciliation.pdf_statement_evidence import (
    PdfDirectionConfidence,
    PdfDirectionSource,
    PdfOriginalAmountSign,
    PdfRowReviewStatus,
)
from finance_core.reconciliation.pdf_statement_extractor import (
    PdfExtractedLine,
    PdfExtractedPage,
    PdfExtractionResult,
)
from finance_core.reconciliation.pdf_statement_template import PdfStatementTemplate
from finance_core.reconciliation.pdf_statement_template_adapter import (
    _DIRECTION_TO_ENUM,
    TemplateAdapterResult,
    _build_source_statement_id,
    _convert_direction,
    convert_parsed_statement_row_to_adapter_row,
    convert_template_parse_result_to_adapter_payload,
)
from finance_core.reconciliation.pdf_statement_template_cli import (
    ParsedStatementRow,
    ParseResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(**overrides) -> ParsedStatementRow:
    """Build a clean ParsedStatementRow with sensible defaults."""
    defaults = {
        "source_path": "/statements/test_stmt.pdf",
        "template_id": "sample_bank_v1",
        "source_account_label": "Test Account",
        "transaction_date": Date(2026, 7, 1),
        "posted_date": Date(2026, 7, 2),
        "description": "Coffee Shop",
        "amount": Decimal("12.50"),
        "currency": "MYR",
        "amount_direction": "DEBIT",
        "raw_line": "01/07 Coffee Shop 12.50",
        "source_page_number": 1,
        "row_index": 0,
        "parse_status": "ok",
        "warnings": (),
        "source_content_hash": "a" * 64,
        "source_filename": "test_stmt.pdf",
        "source_row_number": 1,
        "source_row_ref": "page-1:line-1",
        "source_text_excerpt": "01/07 Coffee Shop 12.50 DEBIT",
        "direction_source": PdfDirectionSource.EXPLICIT_TOKEN,
        "direction_confidence": PdfDirectionConfidence.HIGH,
        "review_status": PdfRowReviewStatus.AUTHORITATIVE,
        "original_amount_token": "12.50",
        "original_amount_sign": PdfOriginalAmountSign.POSITIVE,
        "currency_token": "MYR",
        "transaction_date_token": "2026-07-01",
        "posted_date_token": "2026-07-02",
    }
    defaults.update(overrides)
    if "source_row_ref" not in overrides:
        defaults["source_row_ref"] = f"row-{defaults['row_index']}"
    if "source_row_number" not in overrides:
        defaults["source_row_number"] = int(defaults["row_index"]) + 1
    if "original_amount_token" not in overrides and defaults["amount"] is not None:
        defaults["original_amount_token"] = str(defaults["amount"])
    if "review_status" not in overrides:
        defaults["review_status"] = {
            "ok": PdfRowReviewStatus.AUTHORITATIVE,
            "warning": PdfRowReviewStatus.REVIEW_REQUIRED,
            "error": PdfRowReviewStatus.REJECTED,
        }[str(defaults["parse_status"])]
    return ParsedStatementRow(**defaults)


def _make_template(**overrides) -> PdfStatementTemplate:
    """Build a minimal PdfStatementTemplate for ParseResult construction."""
    defaults = {
        "template_id": "sample_bank_v1",
        "institution_name": "Test Bank",
        "statement_type": "bank",
        "account_label": "Test Account",
        "currency": "MYR",
        "date_format": "%d/%m/%Y",
    }
    defaults.update(overrides)
    return PdfStatementTemplate(**defaults)


def _make_empty_extraction() -> PdfExtractionResult:
    """Build a minimal successful extraction result."""
    return PdfExtractionResult(
        pdf_path="/statements/test_stmt.pdf",
        pages=(
            PdfExtractedPage(
                page_number=1,
                lines=(PdfExtractedLine(text="test", line_number=1),),
                raw_text_block="test",
                warnings=(),
            ),
        ),
        total_pages=1,
        total_lines=1,
        warnings=(),
        success=True,
        error_message="",
    )


def _make_parse_result(rows: tuple[ParsedStatementRow, ...]) -> ParseResult:
    """Build a ParseResult from rows for aggregate tests."""
    template = _make_template()
    return ParseResult(
        pdf_path="/statements/test_stmt.pdf",
        template_id="sample_bank_v1",
        template=template,
        rows=rows,
        total_rows=len(rows),
        ok_count=sum(1 for r in rows if r.parse_status == "ok"),
        warning_count=sum(1 for r in rows if r.parse_status == "warning"),
        error_count=sum(1 for r in rows if r.parse_status == "error"),
        extraction=_make_empty_extraction(),
        extraction_success=True,
        extraction_warnings=(),
    )


# ---------------------------------------------------------------------------
# Source statement ID
# ---------------------------------------------------------------------------


class TestBuildSourceStatementId:
    """Tests for _build_source_statement_id."""

    def test_deterministic_same_input(self):
        a = _build_source_statement_id("/path/a.pdf", "t1")
        b = _build_source_statement_id("/path/a.pdf", "t1")
        assert a == b

    def test_different_template_produces_different_id(self):
        a = _build_source_statement_id("/path/a.pdf", "t1")
        b = _build_source_statement_id("/path/a.pdf", "t2")
        assert a != b

    def test_different_path_produces_different_id(self):
        a = _build_source_statement_id("/path/a.pdf", "t1")
        b = _build_source_statement_id("/path/b.pdf", "t1")
        assert a != b

    def test_prefix_format(self):
        sid = _build_source_statement_id("/path/a.pdf", "sample_bank_v1")
        assert sid.startswith("pdf-tmpl-cli-")
        assert len(sid) > len("pdf-tmpl-cli-")


# ---------------------------------------------------------------------------
# Direction conversion
# ---------------------------------------------------------------------------


class TestConvertDirection:
    """Tests for _convert_direction."""

    def test_debit_to_enum(self):
        assert _convert_direction("DEBIT") == StatementAmountDirection.DEBIT

    def test_credit_to_enum(self):
        assert _convert_direction("CREDIT") == StatementAmountDirection.CREDIT

    def test_refund_to_enum(self):
        assert _convert_direction("REFUND") == StatementAmountDirection.REFUND

    def test_payment_to_enum(self):
        assert _convert_direction("PAYMENT") == StatementAmountDirection.PAYMENT

    def test_fee_to_enum(self):
        assert _convert_direction("FEE") == StatementAmountDirection.FEE

    def test_interest_to_enum(self):
        assert _convert_direction("INTEREST") == StatementAmountDirection.INTEREST

    def test_unknown_to_enum(self):
        assert _convert_direction("UNKNOWN") == StatementAmountDirection.UNKNOWN

    def test_unrecognised_string_defaults_to_unknown(self):
        assert _convert_direction("GARBAGE") == StatementAmountDirection.UNKNOWN

    def test_empty_string_defaults_to_unknown(self):
        assert _convert_direction("") == StatementAmountDirection.UNKNOWN


# ---------------------------------------------------------------------------
# Single row conversion
# ---------------------------------------------------------------------------


class TestConvertSingleRow:
    """Tests for convert_parsed_statement_row_to_adapter_row."""

    def test_clean_row_converts_successfully(self):
        row = _make_row()
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert isinstance(result, ParsedPdfStatementRow)
        assert result.source_statement_id == "stmt-001"
        assert result.description == "Coffee Shop"
        assert result.amount == Decimal("12.50")

    def test_source_path_becomes_attachment_path(self):
        row = _make_row(source_path="/statements/bank.pdf")
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.attachment_path == "/statements/bank.pdf"

    def test_raw_line_becomes_raw_row_text(self):
        row = _make_row(raw_line="01/07 Coffee  12.50 D")
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.raw_row_text == "01/07 Coffee  12.50 D"

    def test_row_index_becomes_source_row_ref(self):
        row = _make_row(row_index=42)
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.source_row_ref == "row-42"

    def test_source_page_number_preserved(self):
        row = _make_row(source_page_number=3)
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.source_page_number == 3

    def test_source_page_number_none_preserved(self):
        row = _make_row(source_page_number=None)
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.source_page_number is None

    def test_transaction_date_preserved(self):
        d = Date(2026, 6, 15)
        row = _make_row(transaction_date=d)
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.transaction_date == d

    def test_posted_date_preserved(self):
        d = Date(2026, 6, 16)
        row = _make_row(posted_date=d)
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.posted_date == d

    def test_posted_date_none_handled(self):
        row = _make_row(posted_date=None)
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.posted_date is None

    def test_both_dates_none_handled(self):
        row = _make_row(transaction_date=None, posted_date=None)
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.transaction_date is None
        assert result.posted_date is None

    def test_amount_preserved_as_absolute(self):
        row = _make_row(amount=Decimal("100.00"))
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.amount == Decimal("100.00")

    def test_amount_none_preserved(self):
        row = _make_row(amount=None)
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.amount is None

    def test_currency_preserved(self):
        row = _make_row(currency="SGD")
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.currency == "SGD"

    def test_debit_direction_maps_correctly(self):
        row = _make_row(amount_direction="DEBIT")
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.amount_direction == StatementAmountDirection.DEBIT

    def test_credit_direction_maps_correctly(self):
        row = _make_row(amount_direction="CREDIT")
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.amount_direction == StatementAmountDirection.CREDIT

    def test_unknown_direction_maps_correctly(self):
        row = _make_row(amount_direction="UNKNOWN")
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.amount_direction == StatementAmountDirection.UNKNOWN

    def test_refund_direction_maps_correctly(self):
        row = _make_row(amount_direction="REFUND")
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.amount_direction == StatementAmountDirection.REFUND

    def test_description_preserved(self):
        row = _make_row(description="Lunch at restaurant")
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.description == "Lunch at restaurant"

    def test_empty_description_preserved(self):
        row = _make_row(description="")
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.description == ""

    def test_external_reference_defaults_to_none(self):
        row = _make_row()
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert result.external_reference is None

    def test_source_statement_id_passed_through(self):
        row = _make_row()
        result = convert_parsed_statement_row_to_adapter_row(row, "my-statement-id")
        assert result.source_statement_id == "my-statement-id"

    def test_row_is_immutable(self):
        row = _make_row()
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        with pytest.raises(Exception):
            result.amount = Decimal("0")


# ---------------------------------------------------------------------------
# Warning row conversion -- preserves parse_status info but still adapts
# ---------------------------------------------------------------------------


class TestWarningRowConversion:
    """Warning rows should still be adapted (not blocked), flowing through
    to the bridge normalizer for validation."""

    def test_warning_row_is_still_adapted(self):
        row = _make_row(
            parse_status="warning",
            warnings=("Could not parse amount from line",),
            amount=None,
        )
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert isinstance(result, ParsedPdfStatementRow)
        assert result.amount is None

    def test_warning_row_counted_in_aggregate(self):
        rows = (_make_row(row_index=0, parse_status="warning", warnings=("warn1",)),)
        pr = _make_parse_result(rows)
        adapter_result = convert_template_parse_result_to_adapter_payload(pr)
        assert adapter_result.warning_count == 1
        assert adapter_result.ok_count == 0
        assert adapter_result.error_count == 0

    def test_warning_row_bridge_blocks_missing_amount(self):
        row = _make_row(parse_status="warning", amount=None)
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        vr = validate_pdf_statement_row(adapted)
        assert vr.is_blocked
        from finance_core.reconciliation.pdf_statement_bridge import PdfBlockedReason

        assert PdfBlockedReason.MISSING_AMOUNT in vr.blocked_reasons


# ---------------------------------------------------------------------------
# Error row conversion -- still adapted, bridge blocks
# ---------------------------------------------------------------------------


class TestErrorRowConversion:
    """Error rows should still be adapted and flow through to the bridge
    normalizer, which will block them for missing critical fields."""

    def test_error_row_is_still_adapted(self):
        row = _make_row(
            parse_status="error",
            warnings=("Too few fields to parse",),
            amount=None,
            transaction_date=None,
        )
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert isinstance(result, ParsedPdfStatementRow)
        assert result.amount is None
        assert result.transaction_date is None

    def test_error_row_counted_in_aggregate(self):
        rows = (_make_row(row_index=0, parse_status="error", warnings=("err1",)),)
        pr = _make_parse_result(rows)
        adapter_result = convert_template_parse_result_to_adapter_payload(pr)
        assert adapter_result.error_count == 1
        assert adapter_result.ok_count == 0
        assert adapter_result.warning_count == 0

    def test_error_row_bridge_blocks_missing_fields(self):
        row = _make_row(
            parse_status="error",
            amount=None,
            transaction_date=None,
            posted_date=None,
        )
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        vr = validate_pdf_statement_row(adapted)
        assert vr.is_blocked

    def test_error_row_does_not_become_final_financial_record(self):
        row = _make_row(parse_status="error", amount=None)
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        vr = validate_pdf_statement_row(adapted)
        assert vr.is_blocked
        assert not vr.is_valid
        assert vr.needs_review


# ---------------------------------------------------------------------------
# Malformed/incomplete row safety
# ---------------------------------------------------------------------------


class TestMalformedRowSafety:
    """Malformed or incomplete rows should not be accepted as valid."""

    def test_row_with_no_amount_blocks_at_bridge(self):
        row = _make_row(amount=None, parse_status="error")
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        vr = validate_pdf_statement_row(adapted)
        assert vr.is_blocked

    def test_row_with_unknown_direction_blocks_at_bridge(self):
        row = _make_row(amount_direction="UNKNOWN")
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        vr = validate_pdf_statement_row(adapted)
        assert vr.is_blocked

    def test_row_with_empty_description_blocks_at_bridge(self):
        row = _make_row(description="")
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        vr = validate_pdf_statement_row(adapted)
        assert vr.is_blocked

    def test_row_with_no_dates_blocks_at_bridge(self):
        row = _make_row(transaction_date=None, posted_date=None)
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        vr = validate_pdf_statement_row(adapted)
        assert vr.is_blocked


# ---------------------------------------------------------------------------
# Aggregate result conversion
# ---------------------------------------------------------------------------


class TestAggregateConversion:
    """Tests for convert_template_parse_result_to_adapter_payload."""

    def test_convert_mixed_rows(self):
        rows = (
            _make_row(row_index=0, parse_status="ok", amount=Decimal("10")),
            _make_row(row_index=1, parse_status="warning", amount=Decimal("20"), warnings=("w",)),
            _make_row(row_index=2, parse_status="error", amount=None, warnings=("e",)),
            _make_row(row_index=3, parse_status="ok", amount=Decimal("30")),
        )
        pr = _make_parse_result(rows)
        result = convert_template_parse_result_to_adapter_payload(pr)
        assert isinstance(result, TemplateAdapterResult)
        assert result.total_rows == 4
        assert result.ok_count == 2
        assert result.warning_count == 1
        assert result.error_count == 1
        assert len(result.adapted_rows) == 4

    def test_convert_empty_result(self):
        pr = _make_parse_result(())
        result = convert_template_parse_result_to_adapter_payload(pr)
        assert result.total_rows == 0
        assert result.ok_count == 0
        assert result.warning_count == 0
        assert result.error_count == 0
        assert len(result.adapted_rows) == 0

    def test_source_path_preserved_in_result(self):
        rows = (_make_row(),)
        pr = _make_parse_result(rows)
        result = convert_template_parse_result_to_adapter_payload(pr)
        assert result.source_path == "/statements/test_stmt.pdf"

    def test_template_id_preserved_in_result(self):
        rows = (_make_row(),)
        pr = _make_parse_result(rows)
        result = convert_template_parse_result_to_adapter_payload(pr)
        assert result.template_id == "sample_bank_v1"

    def test_review_only_flag_is_true(self):
        rows = (_make_row(),)
        pr = _make_parse_result(rows)
        result = convert_template_parse_result_to_adapter_payload(pr)
        assert result.review_only is True

    def test_not_final_financial_record_flag_is_true(self):
        rows = (_make_row(),)
        pr = _make_parse_result(rows)
        result = convert_template_parse_result_to_adapter_payload(pr)
        assert result.not_final_financial_record is True

    def test_source_statement_id_is_deterministic(self):
        rows = (_make_row(),)
        pr = _make_parse_result(rows)
        a = convert_template_parse_result_to_adapter_payload(pr)
        b = convert_template_parse_result_to_adapter_payload(pr)
        assert a.source_statement_id == b.source_statement_id

    def test_source_statement_id_excludes_path(self):
        rows = (_make_row(),)
        pr1 = _make_parse_result(rows)
        pr1 = ParseResult(
            pdf_path="/statements/a.pdf",
            template_id="sample_bank_v1",
            template=_make_template(),
            rows=rows,
            total_rows=1,
            ok_count=1,
            warning_count=0,
            error_count=0,
            extraction=_make_empty_extraction(),
            extraction_success=True,
            extraction_warnings=(),
        )
        pr2 = ParseResult(
            pdf_path="/statements/b.pdf",
            template_id="sample_bank_v1",
            template=_make_template(),
            rows=rows,
            total_rows=1,
            ok_count=1,
            warning_count=0,
            error_count=0,
            extraction=_make_empty_extraction(),
            extraction_success=True,
            extraction_warnings=(),
        )
        a = convert_template_parse_result_to_adapter_payload(pr1)
        b = convert_template_parse_result_to_adapter_payload(pr2)
        assert a.source_statement_id == b.source_statement_id

    def test_adapted_rows_ordering_preserved(self):
        rows = tuple(_make_row(row_index=i, description=f"row{i}") for i in range(5))
        pr = _make_parse_result(rows)
        result = convert_template_parse_result_to_adapter_payload(pr)
        for i, adapted in enumerate(result.adapted_rows):
            assert adapted.description == f"row{i}"

    def test_aggregate_result_is_immutable(self):
        rows = (_make_row(),)
        pr = _make_parse_result(rows)
        result = convert_template_parse_result_to_adapter_payload(pr)
        with pytest.raises(Exception):
            result.review_only = False


# ---------------------------------------------------------------------------
# Evidence / audit field preservation
# ---------------------------------------------------------------------------


class TestEvidencePreservation:
    """Tests that source evidence fields survive the adapter conversion."""

    def test_source_path_preserved_in_adapted_row(self):
        row = _make_row(source_path="/evidence/stmt.pdf")
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert adapted.attachment_path == "/evidence/stmt.pdf"

    def test_raw_line_preserved_in_adapted_row(self):
        row = _make_row(raw_line="01/07/2026  Coffee  5.50  D")
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert adapted.raw_row_text == "01/07/2026  Coffee  5.50  D"

    def test_row_index_preserved_as_source_row_ref(self):
        row = _make_row(row_index=7)
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert adapted.source_row_ref == "row-7"

    def test_source_page_number_preserved(self):
        row = _make_row(source_page_number=5)
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert adapted.source_page_number == 5


# ---------------------------------------------------------------------------
# Amount semantics -- conservative, no re-interpretation
# ---------------------------------------------------------------------------


class TestAmountSemantics:
    """Amount and direction must pass through conservatively."""

    def test_absolute_amount_preserved(self):
        row = _make_row(amount=Decimal("99.99"), amount_direction="DEBIT")
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert adapted.amount == Decimal("99.99")
        assert adapted.amount_direction == StatementAmountDirection.DEBIT

    def test_none_amount_preserved(self):
        row = _make_row(amount=None, amount_direction="CREDIT")
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert adapted.amount is None
        assert adapted.amount_direction == StatementAmountDirection.CREDIT

    def test_amount_not_reinterpreted(self):
        row = _make_row(amount=Decimal("50.00"), amount_direction="REFUND")
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert adapted.amount == Decimal("50.00")
        assert adapted.amount_direction == StatementAmountDirection.REFUND

    def test_debit_amount_with_payment_direction(self):
        row = _make_row(amount=Decimal("150.00"), amount_direction="PAYMENT")
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert adapted.amount == Decimal("150.00")
        assert adapted.amount_direction == StatementAmountDirection.PAYMENT


# ---------------------------------------------------------------------------
# Compatibility with bridge helper
# ---------------------------------------------------------------------------


class TestBridgeCompatibility:
    """Adapted rows must be usable with the bridge helper/contract."""

    def test_adapted_row_passes_validate(self):
        row = _make_row()
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        vr = validate_pdf_statement_row(adapted)
        assert vr.is_valid
        assert not vr.is_blocked

    def test_adapted_row_produces_fingerprint(self):
        row = _make_row()
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        fp = build_pdf_statement_row_fingerprint(adapted)
        assert len(fp) == 64
        int(fp, 16)

    def test_adapted_rows_feed_batch_normalizer(self):
        rows = (
            _make_row(row_index=0, parse_status="ok", amount=Decimal("10")),
            _make_row(row_index=1, parse_status="ok", amount=Decimal("20")),
        )
        pr = _make_parse_result(rows)
        adapter_result = convert_template_parse_result_to_adapter_payload(pr)
        batch_result = normalize_pdf_statement_rows_batch(adapter_result.adapted_rows)
        assert batch_result.accepted_count == 2
        assert batch_result.blocked_count == 0

    def test_batch_normalizer_blocks_error_row(self):
        rows = (
            _make_row(row_index=0, parse_status="ok", amount=Decimal("10")),
            _make_row(row_index=1, parse_status="error", amount=None, transaction_date=None),
        )
        pr = _make_parse_result(rows)
        adapter_result = convert_template_parse_result_to_adapter_payload(pr)
        batch_result = normalize_pdf_statement_rows_batch(adapter_result.adapted_rows)
        assert batch_result.accepted_count == 1
        assert batch_result.blocked_count == 1

    def test_batch_normalizer_blocks_unknown_direction(self):
        rows = (_make_row(row_index=0, amount_direction="UNKNOWN", amount=Decimal("10")),)
        pr = _make_parse_result(rows)
        adapter_result = convert_template_parse_result_to_adapter_payload(pr)
        batch_result = normalize_pdf_statement_rows_batch(adapter_result.adapted_rows)
        assert batch_result.blocked_count == 1

    def test_fingerprints_are_deterministic(self):
        row = _make_row()
        a1 = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        a2 = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert build_pdf_statement_row_fingerprint(a1) == build_pdf_statement_row_fingerprint(a2)


# ---------------------------------------------------------------------------
# No live DB behavior
# ---------------------------------------------------------------------------


class TestNoLiveDb:
    """The adapter must never touch a database."""

    def test_no_sqlite_import_in_module(self):
        import finance_core.reconciliation.pdf_statement_template_adapter as m

        assert not hasattr(m, "sqlite3")
        assert "sqlite3" not in dir(m)

    def test_adapter_result_has_no_db_connection(self):
        rows = (_make_row(),)
        pr = _make_parse_result(rows)
        result = convert_template_parse_result_to_adapter_payload(pr)
        assert not hasattr(result, "connection")
        assert not hasattr(result, "cursor")

    def test_single_row_conversion_no_db(self):
        row = _make_row()
        result = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert not hasattr(result, "connection")

    def test_adapted_row_has_no_db_reference(self):
        row = _make_row()
        adapted = convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        for attr in dir(adapted):
            assert "db" not in attr.lower() or attr == "description"


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    """All exported dataclasses must be frozen."""

    def test_template_adapter_result_is_frozen(self):
        rows = (_make_row(),)
        pr = _make_parse_result(rows)
        result = convert_template_parse_result_to_adapter_payload(pr)
        with pytest.raises(Exception):
            result.total_rows = 999

    def test_input_row_not_mutated(self):
        row = _make_row(parse_status="ok")
        original_desc = row.description
        convert_parsed_statement_row_to_adapter_row(row, "stmt-001")
        assert row.description == original_desc

    def test_parse_result_not_mutated(self):
        rows = (_make_row(),)
        pr = _make_parse_result(rows)
        original_count = pr.total_rows
        convert_template_parse_result_to_adapter_payload(pr)
        assert pr.total_rows == original_count


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    """Verify the module's __all__ matches actual public API."""

    def test_all_exports_match(self):
        from finance_core.reconciliation.pdf_statement_template_adapter import __all__

        expected = {
            "TemplateAdapterResult",
            "convert_parsed_statement_row_to_adapter_row",
            "convert_template_parse_result_to_adapter_payload",
        }
        assert set(__all__) == expected


# ---------------------------------------------------------------------------
# Direction map completeness
# ---------------------------------------------------------------------------


class TestDirectionMap:
    """The internal _DIRECTION_TO_ENUM map must be frozen and correct."""

    def test_direction_map_is_frozen(self):
        assert len(_DIRECTION_TO_ENUM) == 16

    def test_all_known_strings_map(self):
        for key in ["DEBIT", "CREDIT", "REFUND", "PAYMENT", "FEE", "INTEREST", "UNKNOWN"]:
            assert key in _DIRECTION_TO_ENUM
            assert isinstance(_DIRECTION_TO_ENUM[key], StatementAmountDirection)

    def test_payment_is_not_debit(self):
        assert _DIRECTION_TO_ENUM["PAYMENT"] == StatementAmountDirection.PAYMENT
        assert _DIRECTION_TO_ENUM["PAYMENT"] != StatementAmountDirection.DEBIT

    def test_refund_is_not_credit(self):
        assert _DIRECTION_TO_ENUM["REFUND"] == StatementAmountDirection.REFUND
        assert _DIRECTION_TO_ENUM["REFUND"] != StatementAmountDirection.CREDIT
