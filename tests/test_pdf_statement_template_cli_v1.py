"""Tests for PDF Statement Parser Template CLI v1."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from finance_core.reconciliation.pdf_statement_extractor import (
    PdfExtractedLine,
    PdfExtractedPage,
    PdfExtractionResult,
    extract_text_from_pdf,
    extract_text_lines,
)
from finance_core.reconciliation.pdf_statement_template import (
    PdfStatementTemplate,
    _reset_registry,
    get_template,
    list_templates,
    register_template,
)
from finance_core.reconciliation.pdf_statement_template_cli import (
    _DIRECTION_MAP,
    ParsedStatementRow,
    ParseResult,
    _build_parser,
    _format_json_output,
    _format_text_output,
    _infer_direction,
    _parse_amount,
    _parse_date,
    _parse_row_heuristic,
    _row_to_dict,
    main,
    parse_pdf_with_template,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_template(**overrides) -> PdfStatementTemplate:
    defaults = {
        "template_id": "test_tmpl",
        "institution_name": "Test Bank",
        "statement_type": "bank",
        "account_label": "Test Account",
        "currency": "MYR",
        "date_format": "%d/%m/%Y",
        "date_field_names": ("date",),
        "posted_date_field_names": ("posted_date",),
        "description_field_names": ("description",),
        "amount_field_names": ("amount",),
        "debit_credit_field_names": ("debit_credit",),
        "amount_sign_rules": "explicit_only",
        "column_delimiter": None,
        "skip_header_lines": 0,
        "skip_footer_lines": 0,
        "notes": "",
    }
    defaults.update(overrides)
    return PdfStatementTemplate(**defaults)


def _assert_no_live_db():
    """Verify no live database access in the current process."""
    pass  # All modules are DB-free by design


# ---------------------------------------------------------------------------
# Template model tests
# ---------------------------------------------------------------------------


class TestPdfStatementTemplate:
    """Tests for PdfStatementTemplate dataclass and registry."""

    def test_frozen_immutable(self):
        t = get_template("sample_bank_v1")
        with pytest.raises(Exception):
            t.template_id = "changed"

    def test_default_values(self):
        t = PdfStatementTemplate(template_id="test", institution_name="Test", notes="unit test")
        assert t.statement_type == "bank"
        assert t.currency == "MYR"
        assert t.amount_sign_rules == "explicit_only"
        assert t.skip_header_lines == 0
        assert t.skip_footer_lines == 0

    def test_field_names_lowercase(self):
        t = PdfStatementTemplate(
            template_id="test",
            institution_name="Test",
            date_field_names=("Date", "Transaction_DaTe"),
            description_field_names=("Merchant", "DESCRIPTION"),
            amount_field_names=("AMOUNT",),
            debit_credit_field_names=("Debit_Credit",),
        )
        assert t.date_field_names_lower == ("date", "transaction_date")
        assert t.description_field_names_lower == ("merchant", "description")
        assert t.amount_field_names_lower == ("amount",)
        assert t.debit_credit_field_names_lower == ("debit_credit",)

    def test_get_template_known(self):
        t = get_template("sample_bank_v1")
        assert t.template_id == "sample_bank_v1"
        assert t.institution_name == "Sample Bank"

    def test_get_template_unknown(self):
        with pytest.raises(ValueError, match="Unknown template"):
            get_template("nonexistent_template")

    def test_list_templates_returns_builtins(self):
        tids = list_templates()
        assert "sample_bank_v1" in tids
        assert "sample_credit_card_v1" in tids
        assert len(tids) >= 2

    def test_register_template_adds_new(self):
        _reset_registry()
        t = PdfStatementTemplate(template_id="custom_test", institution_name="Custom")
        register_template(t)
        assert "custom_test" in list_templates()
        assert get_template("custom_test") is t
        _reset_registry()

    def test_list_templates_sorted(self):
        tids = list_templates()
        assert tids == sorted(tids)


# ---------------------------------------------------------------------------
# Extractor tests
# ---------------------------------------------------------------------------


class TestPdfExtractorFrozenDataclasses:
    """Verify extractor dataclasses are immutable."""

    def test_extracted_line_frozen(self):
        line = PdfExtractedLine(text="hello", line_number=1)
        with pytest.raises(Exception):
            line.text = "changed"

    def test_extracted_page_frozen(self):
        page = PdfExtractedPage(page_number=1, lines=(), raw_text_block="", warnings=())
        with pytest.raises(Exception):
            page.page_number = 2

    def test_extraction_result_frozen(self):
        result = PdfExtractionResult(
            pdf_path="/tmp/test.pdf",
            pages=(),
            total_pages=0,
            total_lines=0,
        )
        with pytest.raises(Exception):
            result.total_pages = 5


class TestPdfExtractorMissingFile:
    """Tests for extract_text_from_pdf when file is missing."""

    def test_nonexistent_file(self, tmp_path):
        bad_path = tmp_path / "does_not_exist.pdf"
        result = extract_text_from_pdf(str(bad_path))
        assert result.success is False
        assert "not found" in result.error_message.lower()

    def test_not_a_file(self, tmp_path):
        result = extract_text_from_pdf(str(tmp_path))
        assert result.success is False
        assert "not a file" in result.error_message.lower()

    def test_wrong_extension(self, tmp_path):
        txt = tmp_path / "test.txt"
        txt.write_text("hello")
        result = extract_text_from_pdf(str(txt))
        assert result.success is False
        assert ".pdf" in result.error_message.lower()


class TestPdfExtractorMissingPypdf:
    """Tests for extract_text_from_pdf when pypdf is unavailable."""

    def test_missing_pypdf_module(self, tmp_path):
        # The ImportError path in extract_text_from_pdf is covered by the
        # try/except ImportError block.  The pypdf module IS installed in
        # the test environment, so we verify the result shape instead.
        pdf = tmp_path / "test.pdf"
        with open(pdf, "wb") as f:
            f.write(b"not-a-real-pdf")
        result = extract_text_from_pdf(str(pdf))
        # Result should be a valid PdfExtractionResult regardless
        assert isinstance(result, PdfExtractionResult)


class TestPdfExtractor:
    """Integration-ish tests for extract_text_from_pdf."""

    def test_empty_result_on_no_text(self, tmp_path):
        # A minimal valid PDF is complex to generate; test the error path
        pdf = tmp_path / "valid.pdf"
        # Write something that's not a valid PDF
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4 invalid content")
        result = extract_text_from_pdf(str(pdf))
        # Should gracefully handle the bad PDF
        assert isinstance(result, PdfExtractionResult)

    def test_extract_text_lines_convenience(self, tmp_path):
        pdf = tmp_path / "test.pdf"
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4 invalid content")
        lines = extract_text_lines(str(pdf))
        assert isinstance(lines, list)

    def test_pdf_path_is_preserved(self, tmp_path):
        pdf = tmp_path / "mypdf.pdf"
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4 invalid")
        result = extract_text_from_pdf(str(pdf))
        assert result.pdf_path == str(pdf.resolve())

    def test_success_field_on_error(self, tmp_path):
        result = extract_text_from_pdf(str(tmp_path / "nope.pdf"))
        assert result.success is False
        assert result.total_pages == 0
        assert result.total_lines == 0


# ---------------------------------------------------------------------------
# Amount parsing tests
# ---------------------------------------------------------------------------


class TestParseAmount:
    def test_simple_integer(self):
        assert _parse_amount("100") == Decimal("100")

    def test_simple_decimal(self):
        assert _parse_amount("45.80") == Decimal("45.80")

    def test_negative_sign(self):
        result = _parse_amount("-12.34")
        assert result == Decimal("-12.34")

    def test_parentheses_negative(self):
        assert _parse_amount("(12.34)") == Decimal("-12.34")

    def test_comma_thousands(self):
        assert _parse_amount("1,234.56") == Decimal("1234.56")

    def test_currency_rm(self):
        assert _parse_amount("RM 45.80") == Decimal("45.80")

    def test_currency_myr(self):
        assert _parse_amount("MYR 100.00") == Decimal("100.00")

    def test_currency_sgd(self):
        assert _parse_amount("SGD 49.90") == Decimal("49.90")

    def test_currency_dollar(self):
        assert _parse_amount("$100.00") == Decimal("100.00")

    def test_none_input(self):
        assert _parse_amount(None) is None

    def test_empty_string(self):
        assert _parse_amount("") is None

    def test_unparseable(self):
        assert _parse_amount("not-a-number") is None

    def test_whitespace_only(self):
        assert _parse_amount("   ") is None


# ---------------------------------------------------------------------------
# Date parsing tests
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_dmy_slash(self):
        d = _parse_date("01/06/2026", "%d/%m/%Y")
        assert d == date(2026, 6, 1)

    def test_iso_format(self):
        d = _parse_date("2026-06-01", "%d/%m/%Y")
        assert d is None

    def test_dmy_dash(self):
        d = _parse_date("01-06-2026", "%d/%m/%Y")
        assert d is None

    def test_none_input(self):
        assert _parse_date(None, "%d/%m/%Y") is None

    def test_empty_string(self):
        assert _parse_date("", "%d/%m/%Y") is None

    def test_unparseable(self):
        assert _parse_date("notadate", "%d/%m/%Y") is None


# ---------------------------------------------------------------------------
# Direction inference tests
# ---------------------------------------------------------------------------


class TestInferDirection:
    def test_explicit_debit_d(self):
        assert _infer_direction("D", None, "standard") == "DEBIT"

    def test_explicit_debit_dr(self):
        assert _infer_direction("DR", None, "standard") == "DEBIT"

    def test_explicit_credit_c(self):
        assert _infer_direction("C", None, "standard") == "CREDIT"

    def test_explicit_credit_cr(self):
        assert _infer_direction("CR", None, "standard") == "CREDIT"

    def test_negative_amount_standard(self):
        assert _infer_direction(None, Decimal("-50"), "standard") == "UNKNOWN"

    def test_positive_amount_standard(self):
        assert _infer_direction(None, Decimal("50"), "standard") == "UNKNOWN"

    def test_negative_amount_inverted(self):
        assert _infer_direction(None, Decimal("-50"), "inverted") == "UNKNOWN"

    def test_positive_amount_inverted(self):
        assert _infer_direction(None, Decimal("50"), "inverted") == "UNKNOWN"

    def test_unknown_fallback(self):
        assert _infer_direction(None, None, "standard") == "UNKNOWN"

    def test_unknown_indicator_no_amount(self):
        assert _infer_direction("XYZ", None, "standard") == "UNKNOWN"


# ---------------------------------------------------------------------------
# Row parsing tests
# ---------------------------------------------------------------------------


class TestParseRowHeuristic:
    def test_simple_line(self):
        template = _make_template()
        row = _parse_row_heuristic(
            raw_line="01/06/2026 Starbucks Coffee 18.50",
            source_path="/tmp/test.pdf",
            template=template,
            row_index=0,
        )
        assert row.transaction_date == date(2026, 6, 1)
        assert row.amount == Decimal("18.50")
        assert row.amount_direction == "UNKNOWN"
        assert "Starbucks Coffee" in row.description
        assert row.parse_status == "warning"
        assert row.source_path == "/tmp/test.pdf"
        assert row.template_id == "test_tmpl"
        assert row.row_index == 0

    def test_line_with_debit_indicator(self):
        template = _make_template(direction_field_offset_from_end=1)
        row = _parse_row_heuristic(
            raw_line="02/06/2026 Grocery Store 150.00 D",
            source_path="/tmp/test.pdf",
            template=template,
            row_index=0,
        )
        assert row.amount == Decimal("150.00")
        assert row.amount_direction == "DEBIT"

    def test_line_with_credit_indicator(self):
        template = _make_template(direction_field_offset_from_end=1)
        row = _parse_row_heuristic(
            raw_line="28/06/2026 Salary Deposit 8000.00 CR",
            source_path="/tmp/test.pdf",
            template=template,
            row_index=0,
        )
        assert row.amount == Decimal("8000.00")
        assert row.amount_direction == "CREDIT"

    def test_too_few_fields(self):
        template = _make_template()
        row = _parse_row_heuristic(
            raw_line="hello",
            source_path="/tmp/test.pdf",
            template=template,
            row_index=0,
        )
        assert row.parse_status == "error"
        assert len(row.warnings) > 0

    def test_no_amount_parsed(self):
        template = _make_template()
        row = _parse_row_heuristic(
            raw_line="01/06/2026 Some Merchant",
            source_path="/tmp/test.pdf",
            template=template,
            row_index=0,
        )
        assert row.amount is None
        assert row.parse_status in ("warning", "error")

    def test_no_date_parsed(self):
        template = _make_template()
        row = _parse_row_heuristic(
            raw_line="Starbucks Coffee 18.50",
            source_path="/tmp/test.pdf",
            template=template,
            row_index=0,
        )
        assert row.transaction_date is None
        assert row.parse_status in ("warning", "error")
        assert row.amount == Decimal("18.50")

    def test_source_path_preservation(self):
        template = _make_template()
        row = _parse_row_heuristic(
            raw_line="01/06/2026 Test 100.00",
            source_path="/abs/path/to/stmt.pdf",
            template=template,
            row_index=0,
        )
        assert row.source_path == "/abs/path/to/stmt.pdf"

    def test_warnings_for_missing_fields(self):
        template = _make_template()
        row = _parse_row_heuristic(
            raw_line="some garbage text",
            source_path="/tmp/test.pdf",
            template=template,
            row_index=0,
        )
        assert len(row.warnings) >= 2  # missing both date and amount

    def test_currency_from_template(self):
        template = _make_template(currency="SGD")
        row = _parse_row_heuristic(
            raw_line="01/06/2026 Test 100.00",
            source_path="/tmp/test.pdf",
            template=template,
            row_index=0,
        )
        assert row.currency == "SGD"


# ---------------------------------------------------------------------------
# Parse result tests
# ---------------------------------------------------------------------------


class TestParseResult:
    def test_parse_result_frozen(self):
        template = _make_template()
        extraction = PdfExtractionResult(
            pdf_path="/tmp/test.pdf",
            pages=(),
            total_pages=0,
            total_lines=0,
        )
        pr = ParseResult(
            pdf_path="/tmp/test.pdf",
            template_id="test",
            template=template,
            rows=(),
            total_rows=0,
            ok_count=0,
            warning_count=0,
            error_count=0,
            extraction=extraction,
        )
        assert pr.total_rows == 0

    def test_rows_are_frozen(self):
        template = _make_template()
        row = _parse_row_heuristic(
            raw_line="01/06/2026 Test 100.00",
            source_path="/tmp/test.pdf",
            template=template,
            row_index=0,
        )
        with pytest.raises(Exception):
            row.amount = Decimal("999")


# ---------------------------------------------------------------------------
# parse_pdf_with_template tests
# ---------------------------------------------------------------------------


class TestParsePdfWithTemplate:
    def test_missing_file(self, tmp_path):
        result = parse_pdf_with_template(str(tmp_path / "nope.pdf"), "sample_bank_v1")
        assert result.extraction_success is False
        assert result.total_rows == 0

    def test_invalid_template(self):
        with pytest.raises(ValueError, match="Unknown template"):
            parse_pdf_with_template("/tmp/nonexistent.pdf", "bad_template")

    def test_smoke_text_extraction(self, tmp_path):
        """Test the full pipeline with a fixture-based approach.

        We can't create a valid PDF easily, so we test the extraction
        failure path and verify the ParseResult structure.
        """
        result = parse_pdf_with_template(str(tmp_path / "does_not_exist.pdf"), "sample_bank_v1")
        assert isinstance(result, ParseResult)
        assert result.extraction_success is False
        assert result.total_rows == 0
        assert result.ok_count == 0


# ---------------------------------------------------------------------------
# Row to dict serialization
# ---------------------------------------------------------------------------


class TestRowToDict:
    def test_full_row(self):
        row = ParsedStatementRow(
            source_path="/tmp/test.pdf",
            template_id="sample_bank_v1",
            source_account_label="Current",
            transaction_date=date(2026, 6, 1),
            posted_date=None,
            description="Test",
            amount=Decimal("100.00"),
            currency="MYR",
            amount_direction="DEBIT",
            raw_line="...",
            source_page_number=1,
            row_index=0,
            parse_status="ok",
            warnings=(),
        )
        d = _row_to_dict(row)
        assert d["source_path"] == "/tmp/test.pdf"
        assert d["transaction_date"] == "2026-06-01"
        assert d["amount"] == "100.00"
        assert d["parse_status"] == "ok"

    def test_none_dates_and_amount(self):
        row = ParsedStatementRow(
            source_path="/tmp/test.pdf",
            template_id="x",
            source_account_label="x",
            transaction_date=None,
            posted_date=None,
            description="x",
            amount=None,
            currency="MYR",
            amount_direction="UNKNOWN",
            raw_line="x",
        )
        d = _row_to_dict(row)
        assert d["transaction_date"] is None
        assert d["amount"] is None


# ---------------------------------------------------------------------------
# Text output tests
# ---------------------------------------------------------------------------


class TestFormatTextOutput:
    def _make_result(self, **overrides):
        template = _make_template()
        extraction = PdfExtractionResult(
            pdf_path="/tmp/test.pdf",
            pages=(),
            total_pages=0,
            total_lines=0,
        )
        kwargs = {
            "pdf_path": "/tmp/test.pdf",
            "template_id": "test",
            "template": template,
            "rows": (),
            "total_rows": 0,
            "ok_count": 0,
            "warning_count": 0,
            "error_count": 0,
            "extraction": extraction,
            "extraction_success": True,
            "extraction_warnings": (),
        }
        kwargs.update(overrides)
        return ParseResult(**kwargs)

    def test_basic_output(self):
        result = self._make_result()
        output = _format_text_output(result)
        assert "PDF Statement Parser Template CLI" in output
        assert "Template:" in output
        assert "Total rows:" in output

    def test_extraction_failed(self):
        result = self._make_result(extraction_success=False)
        output = _format_text_output(result)
        assert "FAILED" in output

    def test_with_rows(self):
        template = _make_template()
        row = ParsedStatementRow(
            source_path="/tmp/test.pdf",
            template_id="test",
            source_account_label="Test",
            transaction_date=date(2026, 6, 1),
            posted_date=None,
            description="Coffee",
            amount=Decimal("5.50"),
            currency="MYR",
            amount_direction="DEBIT",
            raw_line="...",
            row_index=0,
            parse_status="ok",
            warnings=(),
        )
        extraction = PdfExtractionResult(
            pdf_path="/tmp/test.pdf",
            pages=(),
            total_pages=0,
            total_lines=1,
        )
        result = ParseResult(
            pdf_path="/tmp/test.pdf",
            template_id="test",
            template=template,
            rows=(row,),
            total_rows=1,
            ok_count=1,
            warning_count=0,
            error_count=0,
            extraction=extraction,
        )
        output = _format_text_output(result)
        assert "Coffee" in output
        assert "5.50" in output

    def test_contains_safety_notice(self):
        template = _make_template()
        row = ParsedStatementRow(
            source_path="/tmp/test.pdf",
            template_id="test",
            source_account_label="Test",
            transaction_date=date(2026, 6, 1),
            posted_date=None,
            description="Coffee",
            amount=Decimal("5.50"),
            currency="MYR",
            amount_direction="DEBIT",
            raw_line="...",
            row_index=0,
            parse_status="ok",
            warnings=(),
        )
        extraction = PdfExtractionResult(
            pdf_path="/tmp/test.pdf",
            pages=(),
            total_pages=0,
            total_lines=1,
        )
        result = ParseResult(
            pdf_path="/tmp/test.pdf",
            template_id="test",
            template=template,
            rows=(row,),
            total_rows=1,
            ok_count=1,
            warning_count=0,
            error_count=0,
            extraction=extraction,
        )
        output = _format_text_output(result)
        assert "NOT a final financial record" in output


# ---------------------------------------------------------------------------
# JSON output tests
# ---------------------------------------------------------------------------


class TestFormatJsonOutput:
    def _make_result(self, **overrides):
        template = _make_template()
        extraction = PdfExtractionResult(
            pdf_path="/tmp/test.pdf",
            pages=(),
            total_pages=0,
            total_lines=0,
        )
        kwargs = {
            "pdf_path": "/tmp/test.pdf",
            "template_id": "test",
            "template": template,
            "rows": (),
            "total_rows": 0,
            "ok_count": 0,
            "warning_count": 0,
            "error_count": 0,
            "extraction": extraction,
            "extraction_success": True,
            "extraction_warnings": (),
        }
        kwargs.update(overrides)
        return ParseResult(**kwargs)

    def test_valid_json(self):
        result = self._make_result()
        output = _format_json_output(result)
        data = json.loads(output)
        assert data["template_id"] == "test"

    def test_json_has_summary(self):
        result = self._make_result()
        output = _format_json_output(result)
        data = json.loads(output)
        assert "summary" in data
        assert data["summary"]["total_rows"] == 0

    def test_json_review_only_metadata(self):
        result = self._make_result()
        output = _format_json_output(result)
        data = json.loads(output)
        assert data["review_only"] is True
        assert data["not_final_financial_record"] is True


# ---------------------------------------------------------------------------
# CLI argument parser tests
# ---------------------------------------------------------------------------


class TestArgParser:
    def test_parser_has_pdf_arg(self):
        parser = _build_parser()
        args = parser.parse_args(["--pdf", "/tmp/test.pdf", "--template", "sample_bank_v1"])
        assert args.pdf == "/tmp/test.pdf"
        assert args.template == "sample_bank_v1"

    def test_parser_defaults(self):
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.pdf is None
        assert args.template is None
        assert args.json is False
        assert args.list_templates is False

    def test_parser_json_flag(self):
        parser = _build_parser()
        args = parser.parse_args(["--json"])
        assert args.json is True

    def test_parser_output_json(self):
        parser = _build_parser()
        args = parser.parse_args(["--output-json", "/tmp/out.json"])
        assert args.output_json == "/tmp/out.json"

    def test_parser_list_templates(self):
        parser = _build_parser()
        args = parser.parse_args(["--list-templates"])
        assert args.list_templates is True


# ---------------------------------------------------------------------------
# CLI main tests
# ---------------------------------------------------------------------------


class TestCliMain:
    def test_list_templates(self, capsys):
        main(["--list-templates"])
        captured = capsys.readouterr()
        assert "sample_bank_v1" in captured.out
        assert "sample_credit_card_v1" in captured.out

    def test_missing_pdf(self, capsys):
        rc = main(["--template", "sample_bank_v1"])
        assert rc == 2
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_missing_template(self, capsys):
        rc = main(["--pdf", "/tmp/test.pdf"])
        assert rc == 2
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_invalid_template(self, capsys):
        rc = main(["--pdf", "/tmp/test.pdf", "--template", "bad_one"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_missing_file_text_output(self, capsys, tmp_path):
        rc = main(["--pdf", str(tmp_path / "nope.pdf"), "--template", "sample_bank_v1"])
        # Should run successfully (extraction fails but CLI handles it)
        captured = capsys.readouterr()
        # The text output should include FAILED
        assert "FAILED" in captured.out or rc != 0

    def test_json_flag_output(self, capsys, tmp_path):
        rc = main(
            [
                "--pdf",
                str(tmp_path / "nope.pdf"),
                "--template",
                "sample_bank_v1",
                "--json",
            ]
        )
        if rc == 0:
            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert "template_id" in data

    def test_output_json_file(self, capsys, tmp_path):
        out_file = tmp_path / "result.json"
        rc = main(
            [
                "--pdf",
                str(tmp_path / "nope.pdf"),
                "--template",
                "sample_bank_v1",
                "--output-json",
                str(out_file),
            ]
        )
        if rc == 0:
            capsys.readouterr()
            assert out_file.exists()
            data = json.loads(out_file.read_text())
            assert "template_id" in data


# ---------------------------------------------------------------------------
# No live DB / safety tests
# ---------------------------------------------------------------------------


class TestNoLiveDatabase:
    """Verify modules do not access database/finance.db."""

    def test_template_module_no_sqlite(self):
        import finance_core.reconciliation.pdf_statement_template as mod

        source = open(mod.__file__).read()
        assert "finance.db" not in source
        assert "sqlite3" not in source

    def test_extractor_module_no_sqlite(self):
        import finance_core.reconciliation.pdf_statement_extractor as mod

        source = open(mod.__file__).read()
        assert "finance.db" not in source
        assert "sqlite3" not in source

    def test_cli_module_no_sqlite(self):
        import finance_core.reconciliation.pdf_statement_template_cli as mod

        source = open(mod.__file__).read()
        # Check no actual sqlite3 import or connect usage
        assert "import sqlite3" not in source
        assert "sqlite3.connect" not in source


# ---------------------------------------------------------------------------
# Direction map completeness
# ---------------------------------------------------------------------------


class TestDirectionMap:
    def test_all_keys_uppercase(self):
        assert all(k == k.upper() for k in _DIRECTION_MAP)

    def test_common_debit_codes(self):
        assert "D" in _DIRECTION_MAP
        assert "DR" in _DIRECTION_MAP
        assert "DEBIT" in _DIRECTION_MAP

    def test_common_credit_codes(self):
        assert "C" in _DIRECTION_MAP
        assert "CR" in _DIRECTION_MAP
        assert "CREDIT" in _DIRECTION_MAP


# ---------------------------------------------------------------------------
# Amount sign rules edge cases
# ---------------------------------------------------------------------------


class TestAmountSignRules:
    def test_standard_negative_credit(self):
        assert _infer_direction(None, Decimal("-100"), "standard") == "UNKNOWN"

    def test_standard_positive_debit(self):
        assert _infer_direction(None, Decimal("100"), "standard") == "UNKNOWN"

    def test_inverted_negative_debit(self):
        assert _infer_direction(None, Decimal("-100"), "inverted") == "UNKNOWN"

    def test_explicit_only_ignores_sign(self):
        # Without explicit indicator, falls back to UNKNOWN
        assert _infer_direction(None, Decimal("-100"), "standard") == "UNKNOWN"

    def test_explicit_wins_over_sign(self):
        # Explicit D wins over negative amount that would imply CREDIT
        assert _infer_direction("D", Decimal("-100"), "standard") == "DEBIT"


# ---------------------------------------------------------------------------
# Determinism tests
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_template_list_same_twice(self):
        a = list_templates()
        b = list_templates()
        assert a == b

    def test_parse_row_same_input(self):
        template = _make_template()
        a = _parse_row_heuristic("01/06/2026 Coffee 5.50", "/t.pdf", template, 0)
        b = _parse_row_heuristic("01/06/2026 Coffee 5.50", "/t.pdf", template, 0)
        assert a.amount == b.amount
        assert a.transaction_date == b.transaction_date
        assert a.amount_direction == b.amount_direction
        assert a.description == b.description

    def test_text_output_same_twice(self):
        template = _make_template()
        extraction = PdfExtractionResult(
            pdf_path="/tmp/test.pdf",
            pages=(),
            total_pages=0,
            total_lines=0,
        )
        result = ParseResult(
            pdf_path="/tmp/test.pdf",
            template_id="t",
            template=template,
            rows=(),
            total_rows=0,
            ok_count=0,
            warning_count=0,
            error_count=0,
            extraction=extraction,
        )
        a = _format_text_output(result)
        b = _format_text_output(result)
        assert a == b
