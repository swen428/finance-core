"""Tests for PDF Statement Text Extraction Adapter v1."""

from __future__ import annotations

from pathlib import Path

import pytest

from finance_core.reconciliation.migrations import LIVE_DB_PATH
from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
    DEFAULT_PDF_FIXTURE_PATH,
    DEFAULT_TEMPLATE_ID,
    assert_safe_temp_db_path,
    build_parse_result_from_extracted_pdf_statement,
    build_parse_result_from_text_fixture,
    import_pdf_statement_fixture_to_temp_db,
    result_to_summary_dict,
)
from finance_core.reconciliation.pdf_text_extraction_adapter import (
    build_parse_result_from_extracted_pdf,
    is_pdf_extraction_supported,
    pdf_parsing_mode,
)
from tests.fixtures.reconciliation.pdf_statement_temp_db.generate_golden_fixtures import (
    build_text_pdf_bytes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_text_based_pdf(lines: list[str], output_path: Path) -> None:
    """Create a strict deterministic text PDF for focused temporary cases."""
    output_path.write_bytes(build_text_pdf_bytes((tuple(lines),)))


# ---------------------------------------------------------------------------
# Adapter functional tests
# ---------------------------------------------------------------------------


class TestAdapterFunctional:
    """Adapter accepts a valid text-based PDF and returns non-empty text."""

    def test_extracts_text_from_generated_text_pdf(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "test_statement.pdf"
        _create_text_based_pdf(
            lines=[
                "01/07/2026 CoffeeShop 12.50 D",
                "02/07/2026 Salary (1000.00) C",
                "03/07/2026 Grocer 45.67 DEBIT",
            ],
            output_path=pdf_path,
        )
        result = build_parse_result_from_extracted_pdf(
            pdf_path=pdf_path,
            template_id=DEFAULT_TEMPLATE_ID,
        )
        assert result.extraction_success is True
        assert str(pdf_path.resolve()) in result.pdf_path
        assert result.template_id == DEFAULT_TEMPLATE_ID

    def test_pdf_extraction_with_all_matching_text_lines(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "multi_line.pdf"
        _create_text_based_pdf(
            lines=[
                "01/07/2026 CoffeeShop 12.50 D",
                "02/07/2026 Salary 1000.00 C",
                "03/07/2026 Grocer 45.67 DEBIT",
            ],
            output_path=pdf_path,
        )
        result = build_parse_result_from_extracted_pdf(
            pdf_path=pdf_path,
            template_id=DEFAULT_TEMPLATE_ID,
        )
        assert result.extraction_success is True
        assert result.total_rows > 0
        assert result.ok_count >= 1

    def test_preserves_source_pdf_path(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "src_path.pdf"
        _create_text_based_pdf(
            lines=["01/07/2026 CoffeeShop 12.50 D"],
            output_path=pdf_path,
        )
        result = build_parse_result_from_extracted_pdf(
            pdf_path=pdf_path,
            template_id=DEFAULT_TEMPLATE_ID,
        )
        expected_path = str(pdf_path.resolve())
        assert result.pdf_path == expected_path
        for row in result.rows:
            assert row.source_path == expected_path

    def test_rejects_missing_file(self) -> None:
        missing_pdf = Path("/tmp/nonexistent_finance_adapter_test_2026.pdf")
        result = build_parse_result_from_extracted_pdf(
            pdf_path=missing_pdf,
            template_id=DEFAULT_TEMPLATE_ID,
        )
        assert result.extraction_success is False
        assert result.total_rows == 0

    def test_rejects_non_pdf_file(self, tmp_path: Path) -> None:
        txt_file = tmp_path / "not_a_pdf.txt"
        txt_file.write_text("This is not a PDF.")
        result = build_parse_result_from_extracted_pdf(
            pdf_path=txt_file,
            template_id=DEFAULT_TEMPLATE_ID,
        )
        assert result.extraction_success is False
        assert result.total_rows == 0

    def test_handles_empty_extraction(self, tmp_path: Path) -> None:
        from pypdf import PdfWriter

        pdf_path = tmp_path / "blank.pdf"
        writer = PdfWriter()
        writer.add_blank_page(612, 792)
        with open(pdf_path, "wb") as f:
            writer.write(f)

        result = build_parse_result_from_extracted_pdf(
            pdf_path=pdf_path,
            template_id=DEFAULT_TEMPLATE_ID,
        )
        # Should not silently succeed: either extraction failed or zero rows
        assert result.extraction_success is False or result.total_rows == 0, (
            f"Empty PDF must not produce rows: total_rows={result.total_rows}, "
            f"extraction_success={result.extraction_success}"
        )


class TestAdapterUtilityFunctions:
    """Utility functions return expected values."""

    def test_is_pdf_extraction_supported(self) -> None:
        assert is_pdf_extraction_supported() is True

    def test_pdf_parsing_mode_returns_pdf_text(self) -> None:
        assert pdf_parsing_mode() == "pdf_text"


class TestBuildParseResultFromExtractedPdfStatement:
    """Fixture wrapper function integrates with the adapter."""

    def test_wrapper_parses_checked_in_golden_pdf(self) -> None:
        result = build_parse_result_from_extracted_pdf_statement(
            pdf_path=DEFAULT_PDF_FIXTURE_PATH,
            template_id=DEFAULT_TEMPLATE_ID,
        )
        assert result.extraction_success is True
        assert result.total_rows == 3

    def test_wrapper_succeeds_with_generated_text_pdf(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "wrapper_test.pdf"
        _create_text_based_pdf(
            lines=["01/07/2026 CoffeeShop 12.50 D"],
            output_path=pdf_path,
        )
        result = build_parse_result_from_extracted_pdf_statement(
            pdf_path=pdf_path,
            template_id=DEFAULT_TEMPLATE_ID,
        )
        assert result.extraction_success is True
        assert result.total_rows > 0


# ---------------------------------------------------------------------------
# Fixture-text compatibility tests
# ---------------------------------------------------------------------------


class TestFixtureTextModeUnchanged:
    """Existing fixture-text path must continue to work unchanged."""

    def test_fixture_text_import_writes_to_temp_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fixture_text.sqlite"
        result = import_pdf_statement_fixture_to_temp_db(
            db_path=db_path,
            source_mode="fixture_text",
        )
        assert len(result.import_batch.inserted_ids) == 3
        assert result.pdf_parsing_mode == "fixture_text"

    def test_fixture_text_parse_still_works(self) -> None:
        result = build_parse_result_from_text_fixture()
        assert result.total_rows == 3
        assert result.ok_count == 3
        assert result.extraction_success is True

    def test_summary_shows_fixture_text_mode(self, tmp_path: Path) -> None:
        result = import_pdf_statement_fixture_to_temp_db(
            db_path=tmp_path / "summary_fixture.sqlite",
            source_mode="fixture_text",
        )
        summary = result_to_summary_dict(result)
        assert summary["pdf_parsing_mode"] == "fixture_text"


# ---------------------------------------------------------------------------
# pdf_text mode tests
# ---------------------------------------------------------------------------


class TestPdfTextModeImport:
    """Import with source_mode=pdf_text uses real PDF text extraction."""

    def test_pdf_text_mode_import_to_temp_db(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "pdf_text_stmt.pdf"
        _create_text_based_pdf(
            lines=[
                "01/07/2026 CoffeeShop 12.50 D",
                "02/07/2026 Salary 1000.00 C",
            ],
            output_path=pdf_path,
        )
        db_path = tmp_path / "pdf_text.sqlite"
        result = import_pdf_statement_fixture_to_temp_db(
            db_path=db_path,
            pdf_path=pdf_path,
            source_mode="pdf_text",
        )
        assert result.pdf_parsing_mode == "pdf_text"
        assert result.db_path == str(db_path.resolve())
        assert len(result.import_batch.inserted_ids) >= 1

    def test_pdf_text_mode_metadata_in_summary(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "metadata_pdf.pdf"
        _create_text_based_pdf(
            lines=["01/07/2026 CoffeeShop 12.50 D"],
            output_path=pdf_path,
        )
        result = import_pdf_statement_fixture_to_temp_db(
            db_path=tmp_path / "metadata.sqlite",
            pdf_path=pdf_path,
            source_mode="pdf_text",
        )
        summary = result_to_summary_dict(result)
        assert summary["pdf_parsing_mode"] == "pdf_text"
        assert summary["review_only"] is True
        assert summary["not_final_financial_record"] is True

    def test_pdf_text_mode_preserves_source_path(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "src_path_pdf.pdf"
        _create_text_based_pdf(
            lines=["01/07/2026 CoffeeShop 12.50 D"],
            output_path=pdf_path,
        )
        result = import_pdf_statement_fixture_to_temp_db(
            db_path=tmp_path / "src_path.sqlite",
            pdf_path=pdf_path,
            source_mode="pdf_text",
        )
        assert result.pdf_path == str(pdf_path.resolve())

    def test_pdf_text_mode_against_unavailable_pdf(self, tmp_path: Path) -> None:
        """Non-existent PDF in pdf_text mode should fail gracefully."""
        missing_pdf = tmp_path / "does_not_exist.pdf"
        with pytest.raises(Exception):
            import_pdf_statement_fixture_to_temp_db(
                db_path=tmp_path / "nope.sqlite",
                pdf_path=missing_pdf,
                source_mode="pdf_text",
            )

    def test_pdf_text_imports_checked_in_golden_pdf(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "adapter_golden_test.sqlite")
            result = import_pdf_statement_fixture_to_temp_db(
                db_path=db_path,
                pdf_path=DEFAULT_PDF_FIXTURE_PATH,
                source_mode="pdf_text",
            )
            assert result.parse_result.total_rows == 3
            assert len(result.import_batch.inserted_ids) == 3


# ---------------------------------------------------------------------------
# Live DB / Live Path Safety
# ---------------------------------------------------------------------------


class TestLiveDbSafety:
    """Tests that refuse database/finance.db."""

    def test_refuses_live_db_in_pdf_text_mode(self) -> None:
        with pytest.raises(ValueError, match="Refusing to use database/finance.db"):
            import_pdf_statement_fixture_to_temp_db(
                db_path=LIVE_DB_PATH,
                source_mode="pdf_text",
            )

    def test_refuses_live_db_in_fixture_text_mode(self) -> None:
        with pytest.raises(ValueError, match="Refusing to use database/finance.db"):
            import_pdf_statement_fixture_to_temp_db(
                db_path=LIVE_DB_PATH,
                source_mode="fixture_text",
            )

    def test_assert_safe_temp_db_path_rejects_finance_db(self) -> None:
        with pytest.raises(ValueError, match="Refusing to use database/finance.db"):
            assert_safe_temp_db_path(LIVE_DB_PATH)

    def test_assert_safe_temp_db_path_rejects_memory_db(self) -> None:
        with pytest.raises(ValueError, match=":memory:"):
            assert_safe_temp_db_path(":memory:")


# ---------------------------------------------------------------------------
# Source mode / metadata preservation
# ---------------------------------------------------------------------------


class TestSourceModeMetadata:
    """source_mode and pdf_parsing_mode metadata preserved correctly."""

    def test_default_source_mode_is_fixture_text(self, tmp_path: Path) -> None:
        result = import_pdf_statement_fixture_to_temp_db(
            db_path=tmp_path / "default.sqlite",
        )
        assert result.pdf_parsing_mode == "fixture_text"

    def test_explicit_fixture_text_source_mode(self, tmp_path: Path) -> None:
        result = import_pdf_statement_fixture_to_temp_db(
            db_path=tmp_path / "explicit_fixture.sqlite",
            source_mode="fixture_text",
        )
        assert result.pdf_parsing_mode == "fixture_text"

    def test_explicit_pdf_text_source_mode(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "expl_pdf.pdf"
        _create_text_based_pdf(
            lines=["01/07/2026 CoffeeShop 12.50 D"],
            output_path=pdf_path,
        )
        result = import_pdf_statement_fixture_to_temp_db(
            db_path=tmp_path / "explicit_pdf.sqlite",
            pdf_path=pdf_path,
            source_mode="pdf_text",
        )
        assert result.pdf_parsing_mode == "pdf_text"


# ---------------------------------------------------------------------------
# Public export verification
# ---------------------------------------------------------------------------


def test_adapter_public_exports() -> None:
    from finance_core.reconciliation import pdf_text_extraction_adapter as mod

    expected = {
        "build_parse_result_from_extracted_pdf",
        "is_pdf_extraction_supported",
        "pdf_parsing_mode",
    }
    actual = set(mod.__all__)
    assert expected.issubset(actual), f"Missing exports: {expected - actual}"


def test_fixture_public_exports() -> None:
    from finance_core.reconciliation import pdf_statement_temp_db_import_fixture as mod

    assert "build_parse_result_from_extracted_pdf_statement" in mod.__all__
    assert "build_parse_result_from_text_fixture" in mod.__all__
    assert "import_pdf_statement_fixture_to_temp_db" in mod.__all__
