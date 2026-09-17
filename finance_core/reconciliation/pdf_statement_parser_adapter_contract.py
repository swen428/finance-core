"""PDF Statement Parser Adapter Contract v1.

Deterministic adapter boundary that defines how future raw PDF parser output
should be adapted into existing ``ParsedPdfStatementRow`` objects before
entering the PDF statement bridge batch normalizer, import review fixture,
or review CLI.

This module does NOT implement real PDF parsing. It only defines and tests
the adapter boundary using synthetic in-memory parser payloads.

Non-goals:

- No raw PDF parsing.
- No OCR.
- No pdfplumber, pytesseract, pypdf, camelot, tabula, or other PDF/OCR deps.
- No database connection or SQL execution.
- No file I/O (does not open ``attachment_path`` or any other file).
- No mutation of final financial records.
- No statement import persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import (
    ParsedPdfStatementRow,
    PdfStatementBatchNormalizationResult,
    normalize_pdf_statement_rows_batch,
)
from finance_core.reconciliation.pdf_statement_evidence import (
    PDF_EVIDENCE_CONTRACT_VERSION,
    PDF_TEMPLATE_PARSER_NAME,
    PDF_TEMPLATE_PARSER_VERSION,
    PDF_TEXT_EXTRACTION_VERSION,
    PdfAmountSignConvention,
    PdfDirectionConfidence,
    PdfDirectionSource,
    PdfRowReviewStatus,
    collect_explicit_direction_evidence,
    original_amount_sign,
    parse_pdf_amount_token,
)
from finance_core.reconciliation.pdf_statement_import_review_fixture import (
    PdfStatementImportReviewFixture,
    build_pdf_statement_import_review_fixture,
    export_pdf_statement_import_review_audit_payload,
    export_pdf_statement_import_review_dashboard_payload,
)

# ---------------------------------------------------------------------------
# Parser payload dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfParserRowPayload:
    """A single synthetic row payload from a PDF parser.

    This is the canonical shape that a PDF parser must produce for each row
    extracted from a statement PDF. All fields are raw strings as they
    appear in the parser output before interpretation.

    Fields
    ------
    source_page_number:
        Page number where this row was found.
    source_row_ref:
        Stable row/line reference within the page.
    transaction_date_raw:
        Raw transaction date string as extracted from the PDF.
    posted_date_raw:
        Raw posted date string as extracted from the PDF.
    description:
        Raw transaction description text.
    merchant_raw:
        Raw merchant name, if already separated by the parser.
    amount_raw:
        Raw amount string as extracted from the PDF.
    currency_raw:
        Raw currency string as extracted from the PDF.
    debit_credit_raw:
        Raw debit/credit indicator string (e.g. "D", "C", "DR", "CR").
    raw_row_text:
        The full raw text of this row as extracted from the PDF.
    """

    source_page_number: int | None = None
    source_row_ref: str | None = None
    transaction_date_raw: str | None = None
    posted_date_raw: str | None = None
    description: str = ""
    merchant_raw: str = ""
    amount_raw: str | None = None
    currency_raw: str = ""
    debit_credit_raw: str | None = None
    raw_row_text: str = ""
    source_row_number: int | None = None
    source_text_excerpt: str = ""
    table_section_id: str | None = None


@dataclass(frozen=True)
class PdfParserStatementPayload:
    """A synthetic PDF statement payload from a parser.

    Represents the full output of a PDF statement parser for a single
    statement document. The ``rows`` field holds the parser output for
    each row found in the document.

    Fields
    ------
    source_statement_id:
        Stable identifier for the source statement.
    attachment_path:
        Path to the original PDF file (metadata only — never opened).
    rows:
        Tuple of ``PdfParserRowPayload`` in document order.
    """

    source_statement_id: str
    attachment_path: str
    rows: tuple[PdfParserRowPayload, ...]
    source_content_hash: str | None = None
    source_filename: str | None = None
    parser_name: str = PDF_TEMPLATE_PARSER_NAME
    parser_version: str = PDF_TEMPLATE_PARSER_VERSION
    template_name: str = "synthetic-parser-contract"
    template_version: str = "pdf-template-v2"
    extraction_version: str = PDF_TEXT_EXTRACTION_VERSION
    evidence_contract_version: str = PDF_EVIDENCE_CONTRACT_VERSION
    amount_sign_convention: PdfAmountSignConvention = PdfAmountSignConvention.UNSIGNED_EXPLICIT


# ---------------------------------------------------------------------------
# Adapter result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfParserAdapterResult:
    """Result of adapting a parser payload to ``ParsedPdfStatementRow`` objects.

    Separates successfully adapted rows from rows that could not be adapted
    due to unparseable fields. No rows are silently dropped — the
    ``blocked_row_indices`` field preserves the indices of rows that need
    human review.

    Fields
    ------
    adapted_rows:
        Tuple of ``ParsedPdfStatementRow`` in the original input order,
        including rows that may later be blocked by the bridge normalizer.
    total_rows:
        Total number of rows in the input payload.
    adapted_count:
        Number of rows successfully adapted.
    blocked_count:
        Number of rows that could not be adapted.
    blocked_row_indices:
        Zero-based indices of blocked rows in the input payload.
    """

    adapted_rows: tuple[ParsedPdfStatementRow, ...]
    total_rows: int
    adapted_count: int
    blocked_count: int
    blocked_row_indices: tuple[int, ...]


# ---------------------------------------------------------------------------
# Adapter reason codes
# ---------------------------------------------------------------------------


class PdfParserAdapterBlockedReason:
    """Stable reason codes for adapter-level blocked rows.

    These are distinct from the bridge-level ``PdfBlockedReason`` codes.
    Adapter reasons reflect parser payload quality issues (unparseable
    fields), whereas bridge reasons reflect financial validation issues
    (missing amounts, unknown direction, etc.).
    """

    UNPARSEABLE_AMOUNT: str = "unparseable_amount"
    UNPARSEABLE_DATE: str = "unparseable_date"
    UNRECOGNISED_DIRECTION: str = "unrecognised_direction"
    MISSING_REQUIRED_FIELD: str = "missing_required_field"


# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

_ISO_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%m/%d/%Y",
)


def _parse_date(raw: str | None) -> date | None:
    """Parse a raw date string into a ``date`` object.

    Supports ISO format and several common bank-statement formats.
    Returns ``None`` when the raw string is ``None``, empty, or
    unparseable.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    for fmt in _ISO_DATE_FORMATS:
        try:
            from datetime import datetime

            dt = datetime.strptime(stripped, fmt)
            return dt.date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Amount parsing helpers
# ---------------------------------------------------------------------------


def _parse_amount(raw: str | None) -> Decimal | None:
    """Parse a raw amount string into a ``Decimal``.

    Strips currency symbols, commas, and whitespace. Handles signed
    amounts (``-50.00``) and parenthesised negatives (``(50.00)``).
    Returns ``None`` for empty or unparseable inputs.
    """
    parsed = parse_pdf_amount_token(raw)
    return parsed.value if parsed is not None else None


# ---------------------------------------------------------------------------
# Direction inference
# ---------------------------------------------------------------------------


def _infer_direction(
    debit_credit_raw: str | None,
    amount: Decimal | None,
) -> StatementAmountDirection:
    """Map an explicit direction token; never infer direction from amount sign."""
    _ = amount
    if debit_credit_raw is None:
        return StatementAmountDirection.UNKNOWN
    evidence = collect_explicit_direction_evidence((debit_credit_raw,))
    return evidence.direction or StatementAmountDirection.UNKNOWN


# ---------------------------------------------------------------------------
# Currency normalisation
# ---------------------------------------------------------------------------


def _normalise_currency(raw: str) -> str:
    """Normalise a raw currency string to uppercase, stripped form."""
    if not raw:
        return ""
    return raw.strip().upper()


# ---------------------------------------------------------------------------
# Description resolution
# ---------------------------------------------------------------------------


def _resolve_description(description: str, merchant_raw: str) -> str:
    """Resolve the effective description from parser fields.

    If ``merchant_raw`` is non-empty, it takes priority as the
    description. Otherwise ``description`` is used.
    """
    if merchant_raw.strip():
        return merchant_raw.strip()
    return description


# ---------------------------------------------------------------------------
# Single-row adaptation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RowAdaptResult:
    """Internal result of adapting a single parser row."""

    row: ParsedPdfStatementRow | None
    is_blocked: bool
    blocked_reason: str | None


def _adapt_single_row(
    source_statement_id: str,
    attachment_path: str,
    parser_row: PdfParserRowPayload,
    *,
    source_content_hash: str | None = None,
    source_filename: str | None = None,
    parser_name: str = PDF_TEMPLATE_PARSER_NAME,
    parser_version: str = PDF_TEMPLATE_PARSER_VERSION,
    template_name: str = "synthetic-parser-contract",
    template_version: str = "pdf-template-v2",
    extraction_version: str = PDF_TEXT_EXTRACTION_VERSION,
    evidence_contract_version: str = PDF_EVIDENCE_CONTRACT_VERSION,
    amount_sign_convention: PdfAmountSignConvention = PdfAmountSignConvention.UNSIGNED_EXPLICIT,
) -> _RowAdaptResult:
    """Adapt a single parser row payload into a ``ParsedPdfStatementRow``.

    Parses dates, amount, currency, and direction from raw string fields.
    If any critical field cannot be parsed, the row is blocked with a
    reason code.

    Critical fields: amount must be parseable.
    """
    # Parse amount
    amount = _parse_amount(parser_row.amount_raw)
    if amount is None and parser_row.amount_raw is not None and parser_row.amount_raw.strip():
        # Amount was provided but unparseable
        return _RowAdaptResult(
            row=None,
            is_blocked=True,
            blocked_reason=PdfParserAdapterBlockedReason.UNPARSEABLE_AMOUNT,
        )

    # Parse dates
    transaction_date = _parse_date(parser_row.transaction_date_raw)
    posted_date = _parse_date(parser_row.posted_date_raw)

    # Infer direction
    direction_evidence = collect_explicit_direction_evidence((parser_row.debit_credit_raw or "",))
    amount_direction = _infer_direction(parser_row.debit_credit_raw, amount)
    if parser_row.debit_credit_raw is None or not parser_row.debit_credit_raw.strip():
        direction_source = PdfDirectionSource.MISSING
        direction_confidence = PdfDirectionConfidence.NONE
        review_status = PdfRowReviewStatus.REVIEW_REQUIRED
        review_reason = "missing_explicit_direction"
    elif direction_evidence.is_ambiguous:
        direction_source = PdfDirectionSource.INVALID
        direction_confidence = PdfDirectionConfidence.NONE
        review_status = PdfRowReviewStatus.REVIEW_REQUIRED
        review_reason = "ambiguous_direction"
    elif amount_direction is StatementAmountDirection.UNKNOWN:
        direction_source = PdfDirectionSource.INVALID
        direction_confidence = PdfDirectionConfidence.NONE
        review_status = PdfRowReviewStatus.REJECTED
        review_reason = "invalid_direction"
    else:
        direction_source = PdfDirectionSource.EXPLICIT_COLUMN
        direction_confidence = PdfDirectionConfidence.HIGH
        review_status = PdfRowReviewStatus.AUTHORITATIVE
        review_reason = None

    # Normalise currency
    currency = _normalise_currency(parser_row.currency_raw)

    # Resolve description
    description = _resolve_description(parser_row.description, parser_row.merchant_raw)

    # Normalise amount to absolute value for ParsedPdfStatementRow
    # (the bridge normalizer handles this, but we store sign-based amounts
    # with direction for consistency)
    normalized_amount = abs(amount) if amount is not None else None

    row = ParsedPdfStatementRow(
        source_statement_id=source_statement_id,
        attachment_path=attachment_path,
        description=description,
        amount=normalized_amount,
        currency=currency,
        source_page_number=parser_row.source_page_number,
        source_row_number=parser_row.source_row_number,
        source_row_ref=parser_row.source_row_ref,
        transaction_date=transaction_date,
        posted_date=posted_date,
        raw_row_text=parser_row.raw_row_text,
        amount_direction=amount_direction,
        source_content_hash=source_content_hash,
        source_filename=source_filename,
        source_text_excerpt=parser_row.source_text_excerpt or parser_row.raw_row_text,
        table_section_id=parser_row.table_section_id,
        parser_name=parser_name,
        parser_version=parser_version,
        template_name=template_name,
        template_version=template_version,
        extraction_version=extraction_version,
        evidence_contract_version=evidence_contract_version,
        direction_source=direction_source,
        direction_confidence=direction_confidence,
        review_status=review_status,
        review_reason=review_reason,
        original_amount_token=parser_row.amount_raw,
        original_amount_sign=original_amount_sign(parser_row.amount_raw, amount),
        amount_sign_convention=amount_sign_convention,
        currency_token=parser_row.currency_raw,
        currency_source="parser_column",
        transaction_date_token=parser_row.transaction_date_raw,
        posted_date_token=parser_row.posted_date_raw,
    )

    return _RowAdaptResult(row=row, is_blocked=False, blocked_reason=None)


# ---------------------------------------------------------------------------
# Public adapter function
# ---------------------------------------------------------------------------


def adapt_pdf_parser_payload_to_statement_rows(
    payload: PdfParserStatementPayload,
) -> PdfParserAdapterResult:
    """Adapt a PDF parser statement payload into ``ParsedPdfStatementRow`` objects.

    Each row in the payload is adapted independently. Rows with unparseable
    critical fields are tracked as blocked but the remaining rows are still
    adapted. No rows are silently dropped.

    Returns a ``PdfParserAdapterResult`` with adapted rows, counts, and
    blocked-row indices.

    The input payload is never mutated.
    """
    adapted: list[ParsedPdfStatementRow] = []
    blocked_indices: list[int] = []

    for i, parser_row in enumerate(payload.rows):
        result = _adapt_single_row(
            source_statement_id=payload.source_statement_id,
            attachment_path=payload.attachment_path,
            parser_row=parser_row,
            source_content_hash=payload.source_content_hash,
            source_filename=payload.source_filename,
            parser_name=payload.parser_name,
            parser_version=payload.parser_version,
            template_name=payload.template_name,
            template_version=payload.template_version,
            extraction_version=payload.extraction_version,
            evidence_contract_version=payload.evidence_contract_version,
            amount_sign_convention=payload.amount_sign_convention,
        )
        if result.is_blocked or result.row is None:
            blocked_indices.append(i)
        else:
            adapted.append(result.row)

    return PdfParserAdapterResult(
        adapted_rows=tuple(adapted),
        total_rows=len(payload.rows),
        adapted_count=len(adapted),
        blocked_count=len(blocked_indices),
        blocked_row_indices=tuple(blocked_indices),
    )


# ---------------------------------------------------------------------------
# Full chain smoke test helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfParserSmokeTestResult:
    """Result of running the full-chain smoke test.

    Carries the results from every stage of the PDF statement pipeline:
    adapter -> batch normalizer -> import review fixture -> dashboard
    and audit payloads.

    Fields
    ------
    adapter_result:
        Raw adapter result from ``adapt_pdf_parser_payload_to_statement_rows``.
    batch_result:
        Batch normalization result from ``normalize_pdf_statement_rows_batch``.
    review_fixture:
        Import review fixture from ``build_pdf_statement_import_review_fixture``.
    dashboard_payload:
        Dashboard-safe export payload.
    audit_payload:
        Audit export payload with source evidence.
    """

    adapter_result: PdfParserAdapterResult
    batch_result: PdfStatementBatchNormalizationResult
    review_fixture: PdfStatementImportReviewFixture
    dashboard_payload: dict[str, Any]
    audit_payload: dict[str, Any]


def run_smoke_test(
    payload: PdfParserStatementPayload,
) -> PdfParserSmokeTestResult:
    """Run the full-chain smoke test on a synthetic parser payload.

    Pipeline:
    1. Adapt parser payload -> ``ParsedPdfStatementRow`` tuple.
    2. Batch-normalise adapted rows.
    3. Build import review fixture.
    4. Export dashboard-safe payload.
    5. Export audit payload.

    This function does not touch a database, read files, or mutate
    final financial records.
    """
    adapter_result = adapt_pdf_parser_payload_to_statement_rows(payload)
    batch_result = normalize_pdf_statement_rows_batch(adapter_result.adapted_rows)
    review_fixture = build_pdf_statement_import_review_fixture(batch_result)
    dashboard_payload = export_pdf_statement_import_review_dashboard_payload(review_fixture)
    audit_payload = export_pdf_statement_import_review_audit_payload(batch_result)

    return PdfParserSmokeTestResult(
        adapter_result=adapter_result,
        batch_result=batch_result,
        review_fixture=review_fixture,
        dashboard_payload=dashboard_payload,
        audit_payload=audit_payload,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PdfParserRowPayload",
    "PdfParserStatementPayload",
    "PdfParserAdapterResult",
    "PdfParserAdapterBlockedReason",
    "PdfParserSmokeTestResult",
    "adapt_pdf_parser_payload_to_statement_rows",
    "run_smoke_test",
]
