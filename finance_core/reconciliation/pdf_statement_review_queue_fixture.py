"""PDF Statement Review Queue Fixture v1.

Deterministic, read-only review queue fixture that accepts adapter output
(``ParsedPdfStatementRow``-compatible rows) and produces a three-way
classification summary: ready_for_import, needs_review, or blocked.

Classification uses the existing bridge ``validate_pdf_statement_row()``
for blocking rules and supplements it with ``parse_status`` / ``warnings``
from the CLI parser to distinguish ``needs_review`` from ``ready_for_import``.

Every output is clearly marked ``review_only = True`` and
``not_final_financial_record = True``.

Non-goals:

- No raw PDF parsing.
- No OCR.
- No database connection or SQL execution.
- No file I/O (does not open ``attachment_path`` or any other file).
- No mutation of final financial records.
- No statement import persistence.
- No settlement obligation generation.
- No reconciliation matching or apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import (
    ParsedPdfStatementRow,
    PdfBlockedReason,
    PdfValidationResult,
    validate_pdf_statement_row,
)

# ---------------------------------------------------------------------------
# Review queue row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfStatementReviewQueueRow:
    """A single row in the PDF statement review queue.

    Carries the full parsed row data, classification, blocked reasons
    (from bridge validation), parser warnings, and review-only guard
    flags so that the review queue fixture output can be safely displayed
    without danger of being misidentified as a final financial record.

    Fields
    ------
    source_statement_id:
        Stable identity for the source statement.
    attachment_path:
        Path to the original PDF file (evidence preservation).
    source_row_ref:
        Row/line reference within the page, if available.
    raw_row_text:
        Raw text extracted from the PDF row (evidence preservation).
    source_page_number:
        Page number within the PDF, if available.
    transaction_date:
        Parsed transaction date, or ``None``.
    posted_date:
        Parsed posted date, or ``None``.
    description:
        Merchant or transaction description text.
    amount:
        Parsed amount (absolute), or ``None``.
    currency:
        Currency code from parsing.
    amount_direction:
        Direction classification (DEBIT, CREDIT, REFUND, etc.).
    classification:
        One of ``"ready_for_import"``, ``"needs_review"``, ``"blocked"``.
    blocked_reasons:
        Stable bridge reason codes when ``classification == "blocked"``.
    warnings:
        Parser warning messages carried through from the CLI parser.
    parse_status:
        The original ``parse_status`` from ``ParsedStatementRow``, if known.
    review_only:
        Always ``True`` -- output is for review, not final records.
    not_final_financial_record:
        Always ``True`` -- output is not a final financial record.
    """

    source_statement_id: str
    attachment_path: str
    source_row_ref: str | None
    raw_row_text: str
    source_page_number: int | None
    transaction_date: date | None
    posted_date: date | None
    description: str
    amount: Decimal | None
    currency: str
    amount_direction: StatementAmountDirection
    classification: Literal["ready_for_import", "needs_review", "blocked"]
    blocked_reasons: tuple[PdfBlockedReason, ...]
    warnings: tuple[str, ...]
    parse_status: str | None = None
    review_only: bool = True
    not_final_financial_record: bool = True


# ---------------------------------------------------------------------------
# Review queue summary
# ---------------------------------------------------------------------------


_Classification = Literal["ready_for_import", "needs_review", "blocked"]


@dataclass(frozen=True)
class PdfStatementReviewQueueSummary:
    """Aggregate summary of a PDF statement review queue.

    Fields
    ------
    total_rows:
        Total number of rows processed.
    ready_for_import_count:
        Number of rows classified as ready_for_import.
    needs_review_count:
        Number of rows classified as needs_review.
    blocked_count:
        Number of rows classified as blocked.
    warnings_count:
        Total number of warning messages across all rows.
    blocked_reason_counts:
        Sorted tuple of (reason, count) for all blocked reasons.
    source_statement_id:
        Stable identity for the source statement.
    attachment_path:
        Path to the original PDF file (evidence).
    template_id:
        The template ID used for parsing, if available.
    review_only:
        Always ``True``.
    not_final_financial_record:
        Always ``True``.
    """

    total_rows: int
    ready_for_import_count: int
    needs_review_count: int
    blocked_count: int
    warnings_count: int
    blocked_reason_counts: tuple[tuple[PdfBlockedReason, int], ...]
    source_statement_id: str
    attachment_path: str
    template_id: str | None = None
    review_only: bool = True
    not_final_financial_record: bool = True


# ---------------------------------------------------------------------------
# Review queue fixture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfStatementReviewQueueFixture:
    """A deterministic, read-only review queue fixture for parsed PDF
    statement rows.

    Produces a three-way classification of every row and a summary with
    aggregate counts, blocked reason breakdown, and guard flags.

    Fields
    ------
    rows:
        Every input row, classified.
    summary:
        Aggregate summary of the review queue.
    """

    rows: tuple[PdfStatementReviewQueueRow, ...]
    summary: PdfStatementReviewQueueSummary


# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------


def _classify_row(
    validation: PdfValidationResult,
    parse_status: str | None,
    has_warnings: bool,
) -> _Classification:
    """Classify a single row as ready_for_import, needs_review, or blocked.

    Classification rules (conservative by default):

    1. Bridge validation says blocked → ``"blocked"``.
    2. ``parse_status == "error"`` → ``"blocked"`` (parser failure).
    3. ``parse_status == "warning"`` and bridge not blocked → ``"needs_review"``.
    4. ``has_warnings`` (any warning messages) and bridge not blocked →
       ``"needs_review"``. This catches rows from adapter paths that carry
       warnings without an explicit parse_status.
    5. Bridge not blocked and no warnings → ``"ready_for_import"``.
    6. If classification is ambiguous, conservatively choose
       ``"needs_review"`` over ``"ready_for_import"``.
    """
    # Bridge says blocked → blocked
    if validation.is_blocked:
        return "blocked"

    # Parser reported an error → blocked (even if bridge didn't block)
    if parse_status == "error":
        return "blocked"

    # Parser warnings → needs_review (structurally usable but uncertain)
    if parse_status == "warning" or has_warnings:
        return "needs_review"

    # Clean row → ready for import
    return "ready_for_import"


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------


def _build_review_queue_row(
    row: ParsedPdfStatementRow,
    *,
    parse_status: str | None = None,
    parser_warnings: tuple[str, ...] = (),
) -> PdfStatementReviewQueueRow:
    """Build a ``PdfStatementReviewQueueRow`` from a parsed PDF row."""
    validation = validate_pdf_statement_row(row)
    has_warnings = len(parser_warnings) > 0
    classification = _classify_row(validation, parse_status, has_warnings)

    return PdfStatementReviewQueueRow(
        source_statement_id=row.source_statement_id,
        attachment_path=row.attachment_path,
        source_row_ref=row.source_row_ref,
        raw_row_text=row.raw_row_text,
        source_page_number=row.source_page_number,
        transaction_date=row.transaction_date,
        posted_date=row.posted_date,
        description=row.description,
        amount=row.amount,
        currency=row.currency,
        amount_direction=row.amount_direction,
        classification=classification,
        blocked_reasons=validation.blocked_reasons,
        warnings=parser_warnings,
        parse_status=parse_status,
        review_only=True,
        not_final_financial_record=True,
    )


def _build_blocked_reason_counts(
    rows: tuple[PdfStatementReviewQueueRow, ...],
) -> tuple[tuple[PdfBlockedReason, int], ...]:
    """Build deterministic, sorted blocked reason counts."""
    counts: dict[PdfBlockedReason, int] = {}
    for r in rows:
        for reason in r.blocked_reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return tuple(sorted(counts.items(), key=lambda item: item[0].value))


def _build_summary(
    rows: tuple[PdfStatementReviewQueueRow, ...],
    source_statement_id: str,
    attachment_path: str,
    template_id: str | None,
) -> PdfStatementReviewQueueSummary:
    """Build the aggregate review queue summary."""
    ready_count = sum(1 for r in rows if r.classification == "ready_for_import")
    review_count = sum(1 for r in rows if r.classification == "needs_review")
    blocked_count = sum(1 for r in rows if r.classification == "blocked")
    warnings_count = sum(len(r.warnings) for r in rows)
    reason_counts = _build_blocked_reason_counts(rows)

    return PdfStatementReviewQueueSummary(
        total_rows=len(rows),
        ready_for_import_count=ready_count,
        needs_review_count=review_count,
        blocked_count=blocked_count,
        warnings_count=warnings_count,
        blocked_reason_counts=reason_counts,
        source_statement_id=source_statement_id,
        attachment_path=attachment_path,
        template_id=template_id,
        review_only=True,
        not_final_financial_record=True,
    )


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_pdf_statement_review_queue(
    rows: "tuple[ParsedPdfStatementRow, ...]",
    source_statement_id: str,
    attachment_path: str,
    template_id: str | None = None,
    *,
    parse_statuses: "dict[str, str] | None" = None,
    parser_warnings: "dict[str, tuple[str, ...]] | None" = None,
) -> PdfStatementReviewQueueFixture:
    """Build a review queue fixture from adapter-output rows.

    Parameters
    ----------
    rows:
        Parsed PDF statement rows from adapter output. Every row is
        classified, regardless of parse state. Input ordering is preserved.
    source_statement_id:
        Stable identity for the source statement.
    attachment_path:
        Path to the original PDF file (evidence).
    template_id:
        The template ID used for parsing, if available.
    parse_statuses:
        Optional mapping of ``source_row_ref`` → parse_status
        (``"ok"``, ``"warning"``, ``"error"``). Used to distinguish
        ``needs_review`` from ``ready_for_import``.
    parser_warnings:
        Optional mapping of ``source_row_ref`` → tuple of warning
        strings. Used to carry CLI parser warnings through to the
        review queue.

    Returns
    -------
    PdfStatementReviewQueueFixture
        Review queue fixture with every row classified and an
        aggregate summary.
    """
    statuses = parse_statuses or {}
    all_warnings = parser_warnings or {}

    result_rows: list[PdfStatementReviewQueueRow] = []
    for row in rows:
        ref = row.source_row_ref or ""
        status = statuses.get(ref)
        warnings = all_warnings.get(ref, ())
        result_rows.append(
            _build_review_queue_row(
                row,
                parse_status=status,
                parser_warnings=warnings,
            )
        )

    rows_tuple = tuple(result_rows)
    summary = _build_summary(rows_tuple, source_statement_id, attachment_path, template_id)

    return PdfStatementReviewQueueFixture(rows=rows_tuple, summary=summary)


# ---------------------------------------------------------------------------
# Text formatter
# ---------------------------------------------------------------------------


def format_pdf_statement_review_queue_text(
    fixture: PdfStatementReviewQueueFixture,
) -> str:
    """Format a review queue fixture as deterministic, human-readable text.

    Output includes:

    - Statement/source identity
    - Summary counts
    - Blocked rows with reasons
    - Warning (needs_review) rows with warning text
    - Ready-for-import rows count

    The format is fixed-width with stable labels and is suitable for
    terminal display, log output, or saving to a text file for human
    review before import approval.
    """
    lines: list[str] = []
    summary = fixture.summary

    # Header
    lines.append("=" * 64)
    lines.append("PDF Statement Review Queue")
    lines.append("=" * 64)
    lines.append("")
    lines.append(f"Source statement:  {summary.source_statement_id}")
    lines.append(f"Attachment:        {summary.attachment_path}")
    if summary.template_id:
        lines.append(f"Template:          {summary.template_id}")
    lines.append("")
    lines.append(f"Total rows:               {summary.total_rows}")
    lines.append(f"  Ready for import:       {summary.ready_for_import_count}")
    lines.append(f"  Needs review:           {summary.needs_review_count}")
    lines.append(f"  Blocked:                {summary.blocked_count}")
    lines.append(f"  Warnings:               {summary.warnings_count}")
    lines.append("")

    guard_text = "review_only=True  not_final_financial_record=True"
    lines.append(guard_text)
    lines.append("")

    # Blocked reason counts
    if summary.blocked_reason_counts:
        lines.append("Blocked reason breakdown:")
        for reason, count in summary.blocked_reason_counts:
            lines.append(f"  {reason.value:<30} {count}")
        lines.append("")

    # Blocked rows
    blocked_rows = [r for r in fixture.rows if r.classification == "blocked"]
    if blocked_rows:
        lines.append("-" * 64)
        lines.append("BLOCKED ROWS")
        lines.append("-" * 64)
        for i, r in enumerate(blocked_rows, 1):
            reasons = ", ".join(rv.value for rv in r.blocked_reasons)
            desc = r.description if r.description else "(empty)"
            ref = r.source_row_ref or "-"
            amt = str(r.amount) if r.amount is not None else "None"
            lines.append(f"  {i:>2}. [{ref}] {desc[:42]:<42} {amt:>10} reasons=[{reasons}]")
        lines.append("")

    # Needs review rows
    review_rows = [r for r in fixture.rows if r.classification == "needs_review"]
    if review_rows:
        lines.append("-" * 64)
        lines.append("NEEDS REVIEW")
        lines.append("-" * 64)
        for i, r in enumerate(review_rows, 1):
            ref = r.source_row_ref or "-"
            desc = r.description if r.description else "(empty)"
            amt = str(r.amount) if r.amount is not None else "None"
            warn_text = "; ".join(r.warnings) if r.warnings else "(no warnings)"
            lines.append(f"  {i:>2}. [{ref}] {desc[:42]:<42} {amt:>10} warnings=[{warn_text}]")
        lines.append("")

    # Ready rows summary only
    ready_count = summary.ready_for_import_count
    lines.append("-" * 64)
    lines.append(f"READY FOR IMPORT: {ready_count} row(s)")
    lines.append("-" * 64)
    lines.append("")
    lines.append("=" * 64)
    lines.append("End of review queue.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PdfStatementReviewQueueRow",
    "PdfStatementReviewQueueSummary",
    "PdfStatementReviewQueueFixture",
    "build_pdf_statement_review_queue",
    "format_pdf_statement_review_queue_text",
]
