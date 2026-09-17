"""PDF Statement Text Extraction Adapter v1.

Narrow, deterministic adapter that bridges the PDF text extractor with the
template-driven statement parser.  This module provides the importable API
that the fixture and bridge paths use when ``--source-mode pdf-text`` is
selected, replacing the fixture-text default with real PDF text extraction.

Adapter flow:
  PDF file path
  -> ``extract_text_from_pdf()``  (pdf_statement_extractor)
  -> template row parsing          (pdf_statement_template_cli)
  -> ``ParseResult`` with source-mode metadata

Non-goals:
- No OCR (image-based PDFs produce empty extraction warnings).
- No table extraction, layout analysis, or position heuristics.
- No AI/model calls.
- No database access or file mutation beyond reading the PDF.
- No final financial record mutation.

Safety:
- Extracted text is treated as raw evidence/input, not final monetary authority.
- Python deterministic normalization (template parser) remains authoritative
  for parsed amounts.
- Source PDF path is preserved as evidence on every result.
- ``pypdf`` unavailability is handled gracefully with a clear error.
"""

from __future__ import annotations

from pathlib import Path

from finance_core.reconciliation.pdf_statement_extractor import (
    DEFAULT_PDF_RESOURCE_LIMITS,
    PdfResourceLimits,
)
from finance_core.reconciliation.pdf_statement_template_cli import (
    ParseResult,
    parse_pdf_with_template,
)


def build_parse_result_from_extracted_pdf(
    pdf_path: str | Path,
    template_id: str,
    *,
    limits: PdfResourceLimits = DEFAULT_PDF_RESOURCE_LIMITS,
) -> ParseResult:
    """Extract text from a real PDF and feed it through the template parser.

    This is the primary adapter entry point.  It calls
    ``parse_pdf_with_template()`` which handles:
    1. Text extraction via ``extract_text_from_pdf()`` (pypdf).
    2. Template-driven line parsing via ``_parse_row_heuristic()``.
    3. Graceful failure reporting for missing files, encrypted PDFs,
       image-only PDFs, and extraction errors.

    Args:
        pdf_path: Path to the PDF statement file.
        template_id: Template ID to use for parsing (e.g. ``sample_bank_v1``).

    Returns:
        A ``ParseResult`` with:
        - ``extraction_success`` indicating whether text was extracted.
        - Source PDF path preserved for evidence.
    """
    return parse_pdf_with_template(
        pdf_path=pdf_path,
        template_id=template_id,
        limits=limits,
    )


def is_pdf_extraction_supported() -> bool:
    """Check whether the pypdf dependency is installed and usable.

    Returns:
        ``True`` when pypdf is importable, ``False`` otherwise.
    """
    try:
        import pypdf  # noqa: F401

        return True
    except ImportError:
        return False


def pdf_parsing_mode() -> str:
    """Return the source-mode label for real PDF extraction.

    The fixture-text path uses ``"fixture_text"``; this adapter uses
    ``"pdf_text"`` to make the distinction deterministic and testable.
    """
    return "pdf_text"


__all__ = [
    "build_parse_result_from_extracted_pdf",
    "is_pdf_extraction_supported",
    "pdf_parsing_mode",
]
