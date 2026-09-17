"""PDF Import Run Report v1.

Deterministic, read-only run report for a complete PDF statement import
run.  Accepts outputs from the existing temp DB import fixture or review
queue bridge and produces a unified, audit-friendly report that summarises
import results, detected issues, skipped/failed rows or statements, source
evidence references, and reconciliation readiness.

The report is additive: it builds on the existing review summary and import
result structures without duplicating parser logic or changing import
semantics.

Non-goals:

- No raw PDF parsing.
- No OCR.
- No database connection or SQL execution unless the underlying import
  result already requires one.
- No mutation of final financial records.
- No settlement obligation generation.
- No AI / model calls.
- No Telegram runtime changes.
- No Metabase runtime / server config changes.

Safety:

- Read-only and deterministic -- repeated calls over the same inputs
  produce stable output.
- Review-only and not-final-financial-record guard flags are always True.
- Dashboard-safe exports exclude audit-only / source-evidence fields.
- Audit exports carry full source evidence but are kept separate.
- No live DB dependency in report construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from finance_core.reconciliation.pdf_statement_review_queue_bridge import (
        PdfStatementReviewQueueBridgeResult,
    )
    from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
        PdfStatementTempDbImportResult,
    )

# ---------------------------------------------------------------------------
# Reconciliation readiness enum
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationReadiness:
    """Stable readiness classification for an import run.

    Fields
    ------
    is_ready:
        True when the run has no blocked rows and at least one row
        ready for import.  Runs with zero ready rows are not ready.
    reason:
        Deterministic human-readable summary of the readiness decision.
    ready_count:
        Number of rows ready for import.
    needs_review_count:
        Number of rows needing human review.
    blocked_count:
        Number of blocked rows.
    """

    is_ready: bool
    reason: str
    ready_count: int
    needs_review_count: int
    blocked_count: int


# ---------------------------------------------------------------------------
# Row-level outcome breakdown
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfImportRunRowOutcomes:
    """Row-level outcome breakdown within a single import run.

    Fields
    ------
    parsed_rows:
        Total rows parsed from the source PDF/text (before
        normalisation).
    accepted_rows:
        Rows that passed validation and normalisation.
    imported_rows:
        Rows successfully inserted into the target table.
    skipped_duplicate_rows:
        Rows that were skipped due to duplicate fingerprints.
    idempotent_rows:
        Rows already present from a previous idempotent call.
    blocked_rows:
        Rows blocked due to validation failures.
    needs_review_rows:
        Rows flagged for human review (not blocked, not ready).
    ready_for_import_rows:
        Rows classified as ready for import.
    """

    parsed_rows: int
    accepted_rows: int
    imported_rows: int
    skipped_duplicate_rows: int
    idempotent_rows: int
    blocked_rows: int
    needs_review_rows: int
    ready_for_import_rows: int


# ---------------------------------------------------------------------------
# Source evidence reference
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfImportRunSourceEvidence:
    """Source evidence references for one import run.

    Fields
    ------
    source_pdf_path:
        Path to the original PDF statement file.
    source_statement_id:
        Stable statement identity from the adapter layer.
    attachment_path:
        Resolved attachment path that was used for import.
    template_id:
        Template ID used for parsing, if available.
    source_mode:
        ``"fixture_text"`` or ``"pdf_text"``.
    source_type:
        Source classification label (e.g. ``bank_statement``).
    """

    source_pdf_path: str
    source_statement_id: str
    attachment_path: str
    template_id: str | None
    source_mode: str
    source_type: str


# ---------------------------------------------------------------------------
# Issue / reason breakdown
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfImportRunIssues:
    """Stable breakdown of blocked reasons and parser warnings.

    Fields
    ------
    blocked_reason_counts:
        Deterministic sorted tuple of (reason, count) pairs using
        stable ``PdfBlockedReason`` value strings.
    parser_warning_counts:
        Deterministic sorted tuple of (warning_text, count) pairs.
    total_blocks:
        Sum of all blocked reason counts.
    total_warnings:
        Sum of all warning counts.
    """

    blocked_reason_counts: tuple[tuple[str, int], ...]
    parser_warning_counts: tuple[tuple[str, int], ...]
    total_blocks: int
    total_warnings: int


# ---------------------------------------------------------------------------
# PdfImportRunReport -- the unified run report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfImportRunReport:
    """Deterministic, read-only run report for a PDF statement import run.

    This is the canonical run report object.  It integrates parse-level
    statistics, import-level outcomes, review classification, blocked/warning
    reason breakdowns, reconciliation readiness, source evidence, and
    guard flags into a single frozen dataclass.

    Fields
    ------
    run_reference:
        Deterministic run identifier derived from batch_public_id.
    batch_public_id:
        Public batch identifier from the import batch.
    source_evidence:
        ``PdfImportRunSourceEvidence`` with full source traceability.
    outcomes:
        ``PdfImportRunRowOutcomes`` with all row-level counts.
    issues:
        ``PdfImportRunIssues`` with blocked reason and warning breakdowns.
    reconciliation_readiness:
        ``ReconciliationReadiness`` classification.
    human_review_needed:
        True when the run has any blocked or needs_review rows.
    review_only:
        Always ``True`` -- output is for review, not final records.
    not_final_financial_record:
        Always ``True`` -- output is not a final financial record.
    parse_warnings:
        Deterministic sorted tuple of per-page/per-row parse warnings.
    extraction_warnings:
        Deterministic sorted tuple of extraction-level warnings.
    created_at_iso:
        ISO-8601 creation timestamp for the run record, if available.
    """

    run_reference: str
    batch_public_id: str
    source_evidence: PdfImportRunSourceEvidence
    outcomes: PdfImportRunRowOutcomes
    issues: PdfImportRunIssues
    reconciliation_readiness: ReconciliationReadiness
    human_review_needed: bool

    review_only: bool = True
    not_final_financial_record: bool = True

    parse_warnings: tuple[str, ...] = ()
    extraction_warnings: tuple[str, ...] = ()
    created_at_iso: str | None = None


# ---------------------------------------------------------------------------
# Reconciliation readiness builder
# ---------------------------------------------------------------------------


def _classify_reconciliation_readiness(
    ready_count: int,
    needs_review_count: int,
    blocked_count: int,
) -> ReconciliationReadiness:
    """Classify reconciliation readiness from row outcome counts.

    Rules:
    1. Any blocked rows -> not ready.
    2. Zero ready rows -> not ready (nothing to reconcile).
    3. Otherwise -> ready (may still have needs_review rows).
    """
    if blocked_count > 0:
        return ReconciliationReadiness(
            is_ready=False,
            reason="blocked_rows_present",
            ready_count=ready_count,
            needs_review_count=needs_review_count,
            blocked_count=blocked_count,
        )
    if ready_count == 0:
        return ReconciliationReadiness(
            is_ready=False,
            reason="no_ready_rows",
            ready_count=0,
            needs_review_count=needs_review_count,
            blocked_count=0,
        )
    return ReconciliationReadiness(
        is_ready=True,
        reason="ready" if needs_review_count == 0 else "ready_with_review_items",
        ready_count=ready_count,
        needs_review_count=needs_review_count,
        blocked_count=0,
    )


# ---------------------------------------------------------------------------
# Builders -- from import result
# ---------------------------------------------------------------------------


def build_pdf_import_run_report_from_import_result(
    import_result: PdfStatementTempDbImportResult,
) -> PdfImportRunReport:
    """Build a ``PdfImportRunReport`` from a ``PdfStatementTempDbImportResult``.

    Parameters
    ----------
    import_result:
        The result of ``import_pdf_statement_fixture_to_temp_db()``.

    Returns
    -------
    PdfImportRunReport
        A deterministic, frozen run report.
    """
    rq = import_result.review_queue
    summary = rq.summary
    batch = import_result.import_batch

    # Source evidence
    source_evidence = PdfImportRunSourceEvidence(
        source_pdf_path=import_result.pdf_path,
        source_statement_id=summary.source_statement_id,
        attachment_path=summary.attachment_path,
        template_id=import_result.parse_result.template_id,
        source_mode=import_result.pdf_parsing_mode,
        source_type=import_result.source_type,
    )

    # Row outcomes
    outcomes = PdfImportRunRowOutcomes(
        parsed_rows=import_result.parse_result.total_rows,
        accepted_rows=import_result.normalization_result.accepted_count,
        imported_rows=len(batch.inserted_ids),
        skipped_duplicate_rows=batch.skipped_duplicates,
        idempotent_rows=batch.idempotent_count,
        blocked_rows=summary.blocked_count,
        needs_review_rows=summary.needs_review_count,
        ready_for_import_rows=summary.ready_for_import_count,
    )

    # Issues / reason breakdowns
    blocked_reasons: list[tuple[str, int]] = [
        (reason.value, count)
        for reason, count in import_result.normalization_result.blocked_reason_counts
    ]
    all_warnings: list[str] = []
    for row in rq.rows:
        all_warnings.extend(row.warnings)
    warning_counts: dict[str, int] = {}
    for w in all_warnings:
        if w:
            warning_counts[w] = warning_counts.get(w, 0) + 1
    sorted_warning_counts: tuple[tuple[str, int], ...] = tuple(sorted(warning_counts.items()))

    issues = PdfImportRunIssues(
        blocked_reason_counts=tuple(blocked_reasons),
        parser_warning_counts=sorted_warning_counts,
        total_blocks=summary.blocked_count,
        total_warnings=len(all_warnings),
    )

    # Reconciliation readiness
    readiness = _classify_reconciliation_readiness(
        ready_count=summary.ready_for_import_count,
        needs_review_count=summary.needs_review_count,
        blocked_count=summary.blocked_count,
    )

    # Human review needed
    human_review_needed = summary.blocked_count > 0 or summary.needs_review_count > 0

    # Parse warnings
    parse_warnings: list[str] = []
    for parse_row in import_result.parse_result.rows:
        if parse_row.warnings:
            parse_warnings.extend(parse_row.warnings)

    # Extraction warnings from parse result
    extraction_warnings: list[str] = []
    if getattr(import_result.parse_result, "extraction_warnings", None):
        extraction_warnings.extend(import_result.parse_result.extraction_warnings)

    return PdfImportRunReport(
        run_reference=import_result.import_batch.public_id,
        batch_public_id=import_result.import_batch.public_id,
        source_evidence=source_evidence,
        outcomes=outcomes,
        issues=issues,
        reconciliation_readiness=readiness,
        human_review_needed=human_review_needed,
        review_only=True,
        not_final_financial_record=True,
        parse_warnings=tuple(sorted(set(parse_warnings))),
        extraction_warnings=tuple(sorted(set(extraction_warnings))),
    )


# ---------------------------------------------------------------------------
# Builders -- from bridge result
# ---------------------------------------------------------------------------


def build_pdf_import_run_report_from_bridge_result(
    bridge_result: PdfStatementReviewQueueBridgeResult,
) -> PdfImportRunReport:
    """Build a ``PdfImportRunReport`` from a ``PdfStatementReviewQueueBridgeResult``.

    This is the recommended entry point when the review queue bridge has
    already been run.  It builds the report from the bridge's import result
    and augments it with bridge-level outcome counts.

    Parameters
    ----------
    bridge_result:
        The result of ``run_pdf_statement_review_queue_bridge()``.

    Returns
    -------
    PdfImportRunReport
        A deterministic, frozen run report.
    """
    report = build_pdf_import_run_report_from_import_result(
        bridge_result.import_result,
    )

    # Augment outcomes with bridge-level counts where they differ
    outcomes = PdfImportRunRowOutcomes(
        parsed_rows=report.outcomes.parsed_rows,
        accepted_rows=bridge_result.matched_count,
        imported_rows=bridge_result.matched_count,
        skipped_duplicate_rows=report.outcomes.skipped_duplicate_rows,
        idempotent_rows=report.outcomes.idempotent_rows,
        blocked_rows=bridge_result.blocked_count,
        needs_review_rows=bridge_result.review_required_count - bridge_result.blocked_count,
        ready_for_import_rows=bridge_result.ready_for_import_count,
    )

    readiness = _classify_reconciliation_readiness(
        ready_count=bridge_result.ready_for_import_count,
        needs_review_count=outcomes.needs_review_rows,
        blocked_count=bridge_result.blocked_count,
    )

    human_review_needed = bridge_result.blocked_count > 0 or outcomes.needs_review_rows > 0

    return PdfImportRunReport(
        run_reference=report.run_reference,
        batch_public_id=report.batch_public_id,
        source_evidence=PdfImportRunSourceEvidence(
            source_pdf_path=report.source_evidence.source_pdf_path,
            source_statement_id=report.source_evidence.source_statement_id,
            attachment_path=report.source_evidence.attachment_path,
            template_id=report.source_evidence.template_id,
            source_mode=bridge_result.source_mode,
            source_type=report.source_evidence.source_type,
        ),
        outcomes=outcomes,
        issues=report.issues,
        reconciliation_readiness=readiness,
        human_review_needed=human_review_needed,
        review_only=True,
        not_final_financial_record=True,
        parse_warnings=report.parse_warnings,
        extraction_warnings=report.extraction_warnings,
    )


# ---------------------------------------------------------------------------
# Export helpers -- dashboard-safe payload
# ---------------------------------------------------------------------------


def export_pdf_import_run_report_dashboard_payload(
    report: PdfImportRunReport,
) -> dict[str, Any]:
    """Export a dashboard-safe run report as a deterministic dict.

    Excludes audit-only fields (source_pdf_path, source_statement_id,
    attachment_path) from the source evidence section.  Reason breakdowns
    use string values for JSON compatibility.

    Parameters
    ----------
    report:
        A ``PdfImportRunReport``.

    Returns
    -------
    dict[str, Any]
        Dashboard-safe dict suitable for JSON serialization.
    """
    payload: dict[str, Any] = {
        "run_reference": report.run_reference,
        "batch_public_id": report.batch_public_id,
        "source_evidence": {
            "source_mode": report.source_evidence.source_mode,
            "source_type": report.source_evidence.source_type,
            "template_id": report.source_evidence.template_id,
        },
        "outcomes": {
            "parsed_rows": report.outcomes.parsed_rows,
            "accepted_rows": report.outcomes.accepted_rows,
            "imported_rows": report.outcomes.imported_rows,
            "skipped_duplicate_rows": report.outcomes.skipped_duplicate_rows,
            "idempotent_rows": report.outcomes.idempotent_rows,
            "blocked_rows": report.outcomes.blocked_rows,
            "needs_review_rows": report.outcomes.needs_review_rows,
            "ready_for_import_rows": report.outcomes.ready_for_import_rows,
        },
        "issues": {
            "blocked_reason_counts": [
                {"reason": reason, "count": count}
                for reason, count in report.issues.blocked_reason_counts
            ],
            "parser_warning_counts": [
                {"warning": warning, "count": count}
                for warning, count in report.issues.parser_warning_counts
            ],
            "total_blocks": report.issues.total_blocks,
            "total_warnings": report.issues.total_warnings,
        },
        "reconciliation_readiness": {
            "is_ready": report.reconciliation_readiness.is_ready,
            "reason": report.reconciliation_readiness.reason,
            "ready_count": report.reconciliation_readiness.ready_count,
            "needs_review_count": report.reconciliation_readiness.needs_review_count,
            "blocked_count": report.reconciliation_readiness.blocked_count,
        },
        "human_review_needed": report.human_review_needed,
        "review_only": report.review_only,
        "not_final_financial_record": report.not_final_financial_record,
    }
    if report.parse_warnings:
        payload["parse_warnings"] = list(report.parse_warnings)
    if report.extraction_warnings:
        payload["extraction_warnings"] = list(report.extraction_warnings)
    if report.created_at_iso is not None:
        payload["created_at_iso"] = report.created_at_iso
    return payload


# ---------------------------------------------------------------------------
# Export helpers -- audit payload
# ---------------------------------------------------------------------------


def export_pdf_import_run_report_audit_payload(
    report: PdfImportRunReport,
) -> dict[str, Any]:
    """Export a full audit run report including source evidence.

    Includes ``source_pdf_path``, ``source_statement_id``, and
    ``attachment_path`` for traceability.  This function is deliberately
    separate from the dashboard-safe export to prevent accidental leakage
    of audit data into dashboard views.

    Parameters
    ----------
    report:
        A ``PdfImportRunReport``.

    Returns
    -------
    dict[str, Any]
        Audit dict suitable for JSON serialization.
    """
    payload = export_pdf_import_run_report_dashboard_payload(report)
    # Add audit-only source evidence fields
    if isinstance(payload.get("source_evidence"), dict):
        payload["source_evidence"] = {
            **payload["source_evidence"],
            "source_pdf_path": report.source_evidence.source_pdf_path,
            "source_statement_id": report.source_evidence.source_statement_id,
            "attachment_path": report.source_evidence.attachment_path,
        }
    return payload


# ---------------------------------------------------------------------------
# Text formatter
# ---------------------------------------------------------------------------


def format_pdf_import_run_report_text(report: PdfImportRunReport) -> str:
    """Format a run report as deterministic human-readable text.

    Output includes run identity, source info, row outcome breakdown,
    blocked reason breakdown, warning breakdowns, reconciliation
    readiness, and guard flags.  Suitable for terminal display, log
    output, or saving to a text file for human review.
    """
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("PDF Import Run Report")
    lines.append("=" * 64)
    lines.append("")
    lines.append(f"Run reference:       {report.run_reference}")
    lines.append(f"Batch public ID:     {report.batch_public_id}")
    lines.append(f"Source mode:         {report.source_evidence.source_mode}")
    lines.append(f"Source type:         {report.source_evidence.source_type}")
    lines.append(f"Source PDF:          {report.source_evidence.source_pdf_path}")
    lines.append(f"Source statement ID: {report.source_evidence.source_statement_id}")
    lines.append(f"Attachment:          {report.source_evidence.attachment_path}")
    if report.source_evidence.template_id:
        lines.append(f"Template ID:         {report.source_evidence.template_id}")
    if report.created_at_iso:
        lines.append(f"Created at:          {report.created_at_iso}")
    lines.append("")

    lines.append("-- Row Outcomes --")
    lines.append(f"  Parsed rows:        {report.outcomes.parsed_rows}")
    lines.append(f"  Accepted (normalised): {report.outcomes.accepted_rows}")
    lines.append(f"  Imported:           {report.outcomes.imported_rows}")
    lines.append(f"  Skipped duplicates: {report.outcomes.skipped_duplicate_rows}")
    lines.append(f"  Idempotent:         {report.outcomes.idempotent_rows}")
    lines.append(f"  Blocked:            {report.outcomes.blocked_rows}")
    lines.append(f"  Needs review:       {report.outcomes.needs_review_rows}")
    lines.append(f"  Ready for import:   {report.outcomes.ready_for_import_rows}")
    lines.append("")

    if report.issues.blocked_reason_counts:
        lines.append("-- Blocked Reason Breakdown --")
        for reason, count in report.issues.blocked_reason_counts:
            lines.append(f"  {reason:<30} {count}")
        lines.append("")

    if report.issues.parser_warning_counts:
        lines.append("-- Parser Warning Breakdown --")
        for warning, count in report.issues.parser_warning_counts:
            lines.append(f"  {warning[:50]:<50} {count}")
        lines.append("")

    if report.parse_warnings:
        lines.append("-- Parse Warnings --")
        for w in report.parse_warnings:
            lines.append(f"  {w}")
        lines.append("")

    if report.extraction_warnings:
        lines.append("-- Extraction Warnings --")
        for w in report.extraction_warnings:
            lines.append(f"  {w}")
        lines.append("")

    lines.append("-- Reconciliation Readiness --")
    lines.append(f"  Ready:              {report.reconciliation_readiness.is_ready}")
    lines.append(f"  Reason:             {report.reconciliation_readiness.reason}")
    lines.append("")

    lines.append(f"Human review needed:  {report.human_review_needed}")
    lines.append(f"review_only:          {report.review_only}")
    lines.append(f"not_final_financial_record: {report.not_final_financial_record}")
    lines.append("")
    lines.append("=" * 64)
    lines.append("End of PDF Import Run Report.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PdfImportRunReport",
    "PdfImportRunRowOutcomes",
    "PdfImportRunSourceEvidence",
    "PdfImportRunIssues",
    "ReconciliationReadiness",
    "build_pdf_import_run_report_from_import_result",
    "build_pdf_import_run_report_from_bridge_result",
    "export_pdf_import_run_report_dashboard_payload",
    "export_pdf_import_run_report_audit_payload",
    "format_pdf_import_run_report_text",
]
