"""PDF Statement Import Run Review Summary v1.

Deterministic, read-only run-level summary for a complete PDF statement
import/review run.  Accepts outputs from the existing temp DB import fixture
or review queue bridge and surfaces a human- and Metabase-friendly summary
that classifies a run as fully_ready, partially_reviewable, or blocked.

Non-goals:

- No raw PDF parsing.
- No OCR.
- No database connection or SQL execution.
- No file I/O.
- No mutation of final financial records.
- No statement import persistence.
- No settlement obligation generation.
- No AI / model calls.
- No Telegram runtime changes.
- No Metabase runtime / server config changes.

Safety:

- Read-only and deterministic -- repeated calls over the same inputs produce
  stable output.
- Review-only guard flags are always True.
- Dashboard-safe exports exclude audit-only / source-evidence fields.
- Audit exports carry full source evidence but are kept separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from finance_core.reconciliation.pdf_statement_review_queue_bridge import (
        PdfStatementReviewQueueBridgeResult,
    )
    from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
        PdfStatementTempDbImportResult,
    )

# ---------------------------------------------------------------------------
# Run status classification
# ---------------------------------------------------------------------------

RunReviewStatus = Literal["fully_ready", "partially_reviewable", "blocked"]

# ---------------------------------------------------------------------------
# Run review summary dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfStatementImportRunReviewSummary:
    """Deterministic, read-only summary of one PDF statement import/review run.

    Fields
    ------
    run_reference:
        Deterministic run identifier derived from batch_public_id.
    batch_public_id:
        Public batch identifier from the import batch.
    source_pdf_path:
        Path to the original PDF statement file (evidence preservation).
    source_statement_id:
        Stable statement identity from the adapter layer.
    template_id:
        Template ID used for parsing, if available.
    source_mode:
        ``"fixture_text"`` (deterministic CI) or ``"pdf_text"``
        (real PDF text extraction).
    pdf_parsing_mode:
        Resolved parsing mode label, matching the import result.
    total_row_count:
        Total number of rows in the review queue.
    ready_for_import_count:
        Number of rows classified as ready_for_import.
    needs_review_count:
        Number of rows classified as needs_review.
    blocked_count:
        Number of rows classified as blocked.
    blocked_reason_counts:
        Deterministic sorted tuple of (reason_str, count) for every
        blocked reason seen.  Reason strings use ``PdfBlockedReason.value``
        for dashboard compatibility.
    warning_reason_counts:
        Deterministic sorted tuple of (warning_text, count) for every
        distinct warning message across all rows.  Empty when there are
        no warnings.
    run_status:
        ``"fully_ready"`` when every row is ready_for_import.
        ``"blocked"`` when at least one row is blocked.
        ``"partially_reviewable"`` when there are needs_review rows
        but no blocked rows.
    persisted_count:
        Number of rows persisted to the review queue temp DB, or 0
        when persistence was not used.
    persistence_run_public_id:
        Stable run public_id for persistence, when available.
    evidence_source_refs:
        Ordered tuple of source evidence references (attachment_path,
        source_statement_id) collected from the review queue.
    review_only:
        Always ``True`` -- output is for review, not final records.
    not_final_financial_record:
        Always ``True`` -- output is not a final financial record.
    """

    # Run identity
    run_reference: str
    batch_public_id: str

    # Source identity
    source_pdf_path: str
    source_statement_id: str
    template_id: str | None
    source_mode: str
    pdf_parsing_mode: str

    # Row classification counts
    total_row_count: int
    ready_for_import_count: int
    needs_review_count: int
    blocked_count: int

    # Reason breakdowns (deterministic, sorted)
    blocked_reason_counts: tuple[tuple[str, int], ...]
    warning_reason_counts: tuple[tuple[str, int], ...]

    # Run status
    run_status: RunReviewStatus

    # Persistence info
    persisted_count: int = 0
    persistence_run_public_id: str | None = None

    # Audit evidence
    evidence_source_refs: tuple[str, ...] = ()

    # Guard flags
    review_only: bool = True
    not_final_financial_record: bool = True


# ---------------------------------------------------------------------------
# Run status classification
# ---------------------------------------------------------------------------


def _classify_run_status(
    ready_count: int,
    needs_review_count: int,
    blocked_count: int,
) -> RunReviewStatus:
    """Classify the overall run status from row classification counts.

    Rules (conservative, in priority order):
    1. Any blocked rows -> ``"blocked"``.
    2. No blocked rows, but needs_review rows -> ``"partially_reviewable"``.
    3. Otherwise -> ``"fully_ready"``.
    """
    if blocked_count > 0:
        return "blocked"
    if needs_review_count > 0:
        return "partially_reviewable"
    return "fully_ready"


# ---------------------------------------------------------------------------
# Warning reason breakdown builder
# ---------------------------------------------------------------------------


def _build_warning_reason_counts(
    warning_texts: tuple[str, ...],
) -> tuple[tuple[str, int], ...]:
    """Build deterministic sorted warning reason counts.

    Parameters
    ----------
    warning_texts:
        All warning message strings collected from review queue rows.
        Order does not matter -- the output is deterministically sorted.

    Returns
    -------
    tuple[tuple[str, int], ...]
        Sorted tuple of (warning_text, count) pairs.  Empty when
        ``warning_texts`` is empty.
    """
    if not warning_texts:
        return ()
    counts: dict[str, int] = {}
    for w in warning_texts:
        if w:
            counts[w] = counts.get(w, 0) + 1
    return tuple(sorted(counts.items()))


# ---------------------------------------------------------------------------
# Builders -- from import result
# ---------------------------------------------------------------------------


def build_import_run_review_summary_from_import_result(
    import_result: "PdfStatementTempDbImportResult",
    *,
    source_mode: str | None = None,
    persisted_count: int = 0,
    persistence_run_public_id: str | None = None,
) -> PdfStatementImportRunReviewSummary:
    """Build a run review summary from a ``PdfStatementTempDbImportResult``.

    Parameters
    ----------
    import_result:
        The result of ``import_pdf_statement_fixture_to_temp_db()``.
    source_mode:
        ``"fixture_text"`` or ``"pdf_text"``.  Defaults to the
        ``pdf_parsing_mode`` from the import result.
    persisted_count:
        Number of rows persisted to review queue, if applicable.
    persistence_run_public_id:
        Stable run public_id for persistence, when available.

    Returns
    -------
    PdfStatementImportRunReviewSummary
    """
    rq = import_result.review_queue
    summary = rq.summary

    # Collect all warning texts from review queue rows
    all_warnings: list[str] = []
    for row in rq.rows:
        all_warnings.extend(row.warnings)

    # Build blocked reason counts using PdfBlockedReason value strings
    blocked_reasons: list[tuple[str, int]] = [
        (reason.value, count) for reason, count in summary.blocked_reason_counts
    ]

    effective_source_mode = source_mode or import_result.pdf_parsing_mode

    # Build evidence source refs
    evidence_refs: list[str] = [summary.attachment_path, summary.source_statement_id]
    if summary.template_id:
        evidence_refs.append(f"template:{summary.template_id}")

    run_status = _classify_run_status(
        summary.ready_for_import_count,
        summary.needs_review_count,
        summary.blocked_count,
    )

    return PdfStatementImportRunReviewSummary(
        run_reference=import_result.import_batch.public_id,
        batch_public_id=import_result.import_batch.public_id,
        source_pdf_path=summary.attachment_path,
        source_statement_id=summary.source_statement_id,
        template_id=summary.template_id,
        source_mode=effective_source_mode,
        pdf_parsing_mode=import_result.pdf_parsing_mode,
        total_row_count=summary.total_rows,
        ready_for_import_count=summary.ready_for_import_count,
        needs_review_count=summary.needs_review_count,
        blocked_count=summary.blocked_count,
        blocked_reason_counts=tuple(blocked_reasons),
        warning_reason_counts=_build_warning_reason_counts(tuple(all_warnings)),
        run_status=run_status,
        persisted_count=persisted_count,
        persistence_run_public_id=persistence_run_public_id,
        evidence_source_refs=tuple(evidence_refs),
        review_only=True,
        not_final_financial_record=True,
    )


# ---------------------------------------------------------------------------
# Builders -- from bridge result
# ---------------------------------------------------------------------------


def build_import_run_review_summary_from_bridge_result(
    bridge_result: "PdfStatementReviewQueueBridgeResult",
) -> PdfStatementImportRunReviewSummary:
    """Build a run review summary from a ``PdfStatementReviewQueueBridgeResult``.

    This is the recommended entry point when the review queue bridge has
    already been run.  It delegates to
    ``build_import_run_review_summary_from_import_result`` with the
    bridge's source_mode, persistence counts, and run public_id.

    Parameters
    ----------
    bridge_result:
        The result of ``run_pdf_statement_review_queue_bridge()``.

    Returns
    -------
    PdfStatementImportRunReviewSummary
    """
    return build_import_run_review_summary_from_import_result(
        import_result=bridge_result.import_result,
        source_mode=bridge_result.source_mode,
        persisted_count=bridge_result.persisted_count,
        persistence_run_public_id=bridge_result.persistence_run_public_id,
    )


# ---------------------------------------------------------------------------
# Export helpers -- dashboard-safe payload
# ---------------------------------------------------------------------------


def export_run_review_summary_dashboard_payload(
    summary: PdfStatementImportRunReviewSummary,
) -> dict[str, Any]:
    """Export a dashboard-safe run review summary as a deterministic dict.

    Excludes audit-only fields (evidence_source_refs, source_pdf_path,
    source_statement_id).
    Reason breakdowns use string values for JSON compatibility.

    Parameters
    ----------
    summary:
        A ``PdfStatementImportRunReviewSummary``.

    Returns
    -------
    dict[str, Any]
        Dashboard-safe dict suitable for JSON serialization.
    """
    payload: dict[str, Any] = {
        "run_reference": summary.run_reference,
        "batch_public_id": summary.batch_public_id,
        "source_mode": summary.source_mode,
        "pdf_parsing_mode": summary.pdf_parsing_mode,
        "template_id": summary.template_id,
        "total_row_count": summary.total_row_count,
        "ready_for_import_count": summary.ready_for_import_count,
        "needs_review_count": summary.needs_review_count,
        "blocked_count": summary.blocked_count,
        "blocked_reason_counts": [
            {"reason": reason, "count": count} for reason, count in summary.blocked_reason_counts
        ],
        "warning_reason_counts": [
            {"warning": warning, "count": count} for warning, count in summary.warning_reason_counts
        ],
        "run_status": summary.run_status,
    }
    if summary.persisted_count > 0:
        payload["persisted_count"] = summary.persisted_count
    if summary.persistence_run_public_id is not None:
        payload["persistence_run_public_id"] = summary.persistence_run_public_id
    payload["review_only"] = summary.review_only
    payload["not_final_financial_record"] = summary.not_final_financial_record
    return payload


# ---------------------------------------------------------------------------
# Export helpers -- audit payload
# ---------------------------------------------------------------------------


def export_run_review_summary_audit_payload(
    summary: PdfStatementImportRunReviewSummary,
) -> dict[str, Any]:
    """Export a full audit run review summary including source evidence.

    Includes ``source_pdf_path``, ``source_statement_id``, and
    ``evidence_source_refs`` for traceability.  This function is
    deliberately separate from the dashboard-safe export to prevent
    accidental leakage of audit data into dashboard views.

    Parameters
    ----------
    summary:
        A ``PdfStatementImportRunReviewSummary``.

    Returns
    -------
    dict[str, Any]
        Audit dict suitable for JSON serialization.
    """
    payload = export_run_review_summary_dashboard_payload(summary)
    # Add audit-only fields
    payload["source_pdf_path"] = summary.source_pdf_path
    payload["source_statement_id"] = summary.source_statement_id
    payload["evidence_source_refs"] = list(summary.evidence_source_refs)
    return payload


# ---------------------------------------------------------------------------
# Text formatter
# ---------------------------------------------------------------------------


def format_import_run_review_summary_text(
    summary: PdfStatementImportRunReviewSummary,
) -> str:
    """Format a run review summary as deterministic human-readable text.

    Output includes run identity, source info, classification counts,
    blocked reason breakdown, warning reason breakdown, run status,
    and guard flags.  Suitable for terminal display, log output, or
    saving to a text file for human review.
    """
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("PDF Statement Import Run Review Summary")
    lines.append("=" * 64)
    lines.append("")
    lines.append(f"Run reference:       {summary.run_reference}")
    lines.append(f"Batch public ID:     {summary.batch_public_id}")
    lines.append(f"Source mode:         {summary.source_mode}")
    lines.append(f"PDF parsing mode:    {summary.pdf_parsing_mode}")
    if summary.template_id:
        lines.append(f"Template ID:         {summary.template_id}")
    lines.append(f"Source PDF:          {summary.source_pdf_path}")
    lines.append(f"Source statement ID: {summary.source_statement_id}")
    lines.append("")
    lines.append(f"Run status:          {summary.run_status}")
    lines.append("")
    lines.append(f"Total rows:          {summary.total_row_count}")
    lines.append(f"  Ready for import:  {summary.ready_for_import_count}")
    lines.append(f"  Needs review:      {summary.needs_review_count}")
    lines.append(f"  Blocked:           {summary.blocked_count}")
    lines.append("")

    if summary.blocked_reason_counts:
        lines.append("Blocked reason breakdown:")
        for reason, count in summary.blocked_reason_counts:
            lines.append(f"  {reason:<30} {count}")
        lines.append("")

    if summary.warning_reason_counts:
        lines.append("Warning reason breakdown:")
        for warning, count in summary.warning_reason_counts:
            lines.append(f"  {warning[:50]:<50} {count}")
        lines.append("")

    if summary.evidence_source_refs:
        lines.append("Evidence source refs:")
        for ref in summary.evidence_source_refs:
            lines.append(f"  {ref}")
        lines.append("")

    if summary.persisted_count > 0:
        lines.append(f"Persisted count:     {summary.persisted_count}")
        if summary.persistence_run_public_id:
            lines.append(f"Persistence run ID:  {summary.persistence_run_public_id}")
        lines.append("")

    lines.append(f"review_only: {summary.review_only}")
    lines.append(f"not_final_financial_record: {summary.not_final_financial_record}")
    lines.append("")
    lines.append("=" * 64)
    lines.append("End of run review summary.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PdfStatementImportRunReviewSummary",
    "RunReviewStatus",
    "build_import_run_review_summary_from_import_result",
    "build_import_run_review_summary_from_bridge_result",
    "export_run_review_summary_dashboard_payload",
    "export_run_review_summary_audit_payload",
    "format_import_run_review_summary_text",
]
