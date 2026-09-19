"""Dependency-sensitive synthetic PDF extraction and evidence identity."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from finance_core.reconciliation import pdf_statement_template_cli
from finance_core.reconciliation.pdf_statement_bridge import (
    build_pdf_statement_row_fingerprint,
    normalize_pdf_statement_row_checked,
)
from finance_core.reconciliation.pdf_statement_extractor import (
    DEFAULT_PDF_RESOURCE_LIMITS,
    PdfExtractionErrorCode,
)
from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
    DEFAULT_TEMPLATE_ID,
    PdfStatementImportExtractionError,
    import_pdf_statement_fixture_to_temp_db,
)
from finance_core.reconciliation.pdf_statement_template_adapter import (
    convert_parsed_statement_row_to_adapter_row,
)

EXPECTED_IDENTITY = "pypdf-6.16.1-text-extraction-v3"


def _write_symbol_statement(path: Path) -> None:
    """Symbol D maps to U+2206 after the dependency's charset correction."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    fonts = DictionaryObject()
    for name, base_font in (("/F1", "/Courier"), ("/F2", "/Symbol")):
        fonts[NameObject(name)] = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject(base_font),
            }
        )
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 10 Tf 72 740 Td (01/07/2026 ) Tj /F2 10 Tf (D) Tj /F1 10 Tf ( 12.50 D) Tj ET\n"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)


def test_symbol_mapping_records_actual_extractor_not_stale_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "synthetic-symbol.pdf"
    _write_symbol_statement(source)
    template = pdf_statement_template_cli.get_template(DEFAULT_TEMPLATE_ID)
    monkeypatch.setattr(
        pdf_statement_template_cli,
        "get_template",
        lambda _: replace(template, extraction_version="pypdf-text-extraction-v2"),
    )
    result = pdf_statement_template_cli.parse_pdf_with_template(source, DEFAULT_TEMPLATE_ID)
    assert result.extraction_success
    assert result.total_rows == 1
    assert result.rows[0].raw_line == "01/07/2026 \u2206 12.50 D"
    assert result.rows[0].amount == Decimal("12.50")
    assert result.rows[0].source_content_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    assert version("pypdf") == "6.16.1"
    assert result.extraction.extraction_version == EXPECTED_IDENTITY
    assert result.template.extraction_version == EXPECTED_IDENTITY
    assert result.rows[0].extraction_version == EXPECTED_IDENTITY
    row = convert_parsed_statement_row_to_adapter_row(result.rows[0], "synthetic-symbol")
    normalized = normalize_pdf_statement_row_checked(row)
    assert normalized.raw_row_payload is not None
    assert normalized.raw_row_payload["extraction_version"] == EXPECTED_IDENTITY
    assert build_pdf_statement_row_fingerprint(row) != build_pdf_statement_row_fingerprint(
        replace(row, extraction_version="pypdf-text-extraction-v2")
    )


def test_symbol_pdf_text_limit_refuses_before_database_creation(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-symbol.pdf"
    _write_symbol_statement(source)
    database = tmp_path / "refused.sqlite"
    with pytest.raises(PdfStatementImportExtractionError) as error:
        import_pdf_statement_fixture_to_temp_db(
            db_path=database,
            pdf_path=source,
            source_mode="pdf_text",
            pdf_limits=replace(DEFAULT_PDF_RESOURCE_LIMITS, max_chars_per_page=5),
        )
    assert error.value.error_code == PdfExtractionErrorCode.PAGE_TEXT_LIMIT_EXCEEDED
    assert not database.exists()
