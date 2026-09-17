"""PDF Import Run Report Production Adapter Interface v1.

Read-only adapter boundary that converts already-computed production
import and bridge result objects (or protocol-compatible equivalents)
into ``PdfImportRunReport`` instances.

The adapter is a thin mapping layer.  It:

* Accepts frozen dataclass inputs that describe import outcomes and
  source evidence.
* Delegates report construction to the existing
  ``PdfImportRunReport`` builder logic -- no duplication.
* Preserves ``review_only=True`` and
  ``not_final_financial_record=True``.
* Does not open database connections, write to SQLite, call AI/OCR,
  trigger settlement, or alter parser/matcher/final-mutation behavior.

Non-goals:

- No production runtime wiring.
- No persistence or migration.
- No live database access.
- No raw PDF parsing or OCR.
- No AI / model calls.
- No final financial record mutation.
- No settlement obligation generation.
- No Telegram / Metabase runtime changes.
- No parser, extractor, matcher, or guarded-apply semantic changes.
- No monetary calculation changes.

Safety:

- Read-only and side-effect-free.
- All guard flags are always True.
- Dashboard-safe and audit payload separation is preserved (reuses
  existing exporters).
- Source evidence is included only in the audit payload path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from finance_core.reconciliation.pdf_import_run_report import (
    PdfImportRunIssues,
    PdfImportRunReport,
    PdfImportRunRowOutcomes,
    PdfImportRunSourceEvidence,
)
from finance_core.reconciliation.pdf_import_run_report import (
    _classify_reconciliation_readiness as _classify_readiness,
)

# ---------------------------------------------------------------------------
# Adapter input types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfImportRunReportProductionAdapterInput:
    """Row-level outcome counts for a production import run.

    This is the minimal set of row-level fields that the adapter
    needs to construct a ``PdfImportRunRowOutcomes`` and derive
    reconciliation readiness.  Any production import result shape
    that can be mapped to this type is accepted.

    Fields
    ------
    parsed_rows:
        Total rows parsed from the source.
    accepted_rows:
        Rows that passed validation / normalisation.
    imported_rows:
        Rows successfully inserted into the target.
    skipped_duplicate_rows:
        Rows skipped because of duplicate fingerprints.
    idempotent_rows:
        Rows already present from a prior idempotent call.
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


@dataclass(frozen=True)
class PdfImportRunReportProductionAdapterSource:
    """Source evidence metadata for a production import run.

    Fields
    ------
    source_pdf_path:
        Path to the original PDF statement file.
    source_statement_id:
        Stable statement identity from the adapter layer.
    attachment_path:
        Resolved attachment path used for import.
    template_id:
        Template ID used for parsing, or ``None``.
    source_mode:
        e.g. ``"fixture_text"``, ``"pdf_text"``.
    source_type:
        Source classification label (e.g. ``"bank_statement"``).
    """

    source_pdf_path: str
    source_statement_id: str
    attachment_path: str
    template_id: str | None
    source_mode: str
    source_type: str


# ---------------------------------------------------------------------------
# Builder: from production import result adapter inputs
# ---------------------------------------------------------------------------


def build_pdf_import_run_report_from_production_import_result(
    *,
    adapter_input: PdfImportRunReportProductionAdapterInput,
    adapter_source: PdfImportRunReportProductionAdapterSource,
    batch_public_id: str,
    issues: PdfImportRunIssues | None = None,
    parse_warnings: tuple[str, ...] = (),
    extraction_warnings: tuple[str, ...] = (),
    created_at_iso: str | None = None,
) -> PdfImportRunReport:
    """Build a ``PdfImportRunReport`` from production adapter inputs.

    This is the main production entry point.  Callers provide
    already-computed outcome counts and source evidence, and the
    adapter constructs a deterministic report.

    Parameters
    ----------
    adapter_input:
        Row-level outcome counts.
    adapter_source:
        Source evidence metadata.
    batch_public_id:
        Public batch identifier for the import run.
    issues:
        Pre-built issue breakdown.  When ``None``, an empty
        ``PdfImportRunIssues`` is used (zero blocks, zero warnings).
    parse_warnings:
        Deduplicated, sorted per-row parse warnings.
    extraction_warnings:
        Deduplicated, sorted extraction-level warnings.
    created_at_iso:
        Optional ISO-8601 creation timestamp.

    Returns
    -------
    PdfImportRunReport
        A deterministic, frozen, read-only run report.
    """
    if issues is None:
        issues = PdfImportRunIssues(
            blocked_reason_counts=(),
            parser_warning_counts=(),
            total_blocks=0,
            total_warnings=0,
        )

    source_evidence = PdfImportRunSourceEvidence(
        source_pdf_path=adapter_source.source_pdf_path,
        source_statement_id=adapter_source.source_statement_id,
        attachment_path=adapter_source.attachment_path,
        template_id=adapter_source.template_id,
        source_mode=adapter_source.source_mode,
        source_type=adapter_source.source_type,
    )

    outcomes = PdfImportRunRowOutcomes(
        parsed_rows=adapter_input.parsed_rows,
        accepted_rows=adapter_input.accepted_rows,
        imported_rows=adapter_input.imported_rows,
        skipped_duplicate_rows=adapter_input.skipped_duplicate_rows,
        idempotent_rows=adapter_input.idempotent_rows,
        blocked_rows=adapter_input.blocked_rows,
        needs_review_rows=adapter_input.needs_review_rows,
        ready_for_import_rows=adapter_input.ready_for_import_rows,
    )

    readiness = _classify_readiness(
        ready_count=adapter_input.ready_for_import_rows,
        needs_review_count=adapter_input.needs_review_rows,
        blocked_count=adapter_input.blocked_rows,
    )

    human_review_needed = adapter_input.blocked_rows > 0 or adapter_input.needs_review_rows > 0

    return PdfImportRunReport(
        run_reference=batch_public_id,
        batch_public_id=batch_public_id,
        source_evidence=source_evidence,
        outcomes=outcomes,
        issues=issues,
        reconciliation_readiness=readiness,
        human_review_needed=human_review_needed,
        review_only=True,
        not_final_financial_record=True,
        parse_warnings=parse_warnings,
        extraction_warnings=extraction_warnings,
        created_at_iso=created_at_iso,
    )


# ---------------------------------------------------------------------------
# Builder: from production bridge result adapter inputs
# ---------------------------------------------------------------------------


def build_pdf_import_run_report_from_production_bridge_result(
    *,
    adapter_input: PdfImportRunReportProductionAdapterInput,
    adapter_source: PdfImportRunReportProductionAdapterSource,
    batch_public_id: str,
    matched_count: int,
    review_required_count: int,
    blocked_count: int,
    ready_for_import_count: int,
    issues: PdfImportRunIssues | None = None,
    parse_warnings: tuple[str, ...] = (),
    extraction_warnings: tuple[str, ...] = (),
    created_at_iso: str | None = None,
) -> PdfImportRunReport:
    """Build a ``PdfImportRunReport`` from production bridge-level counts.

    This function accepts bridge-level outcome counts that may differ from
    the underlying import result.  The resulting report reflects the
    post-bridge state.

    Parameters
    ----------
    adapter_input:
        Row-level outcome counts from the underlying import.
    adapter_source:
        Source evidence metadata.
    batch_public_id:
        Public batch identifier for the import run.
    matched_count:
        Number of rows successfully normalised and inserted during
        the bridge step.
    review_required_count:
        Number of rows requiring human review (blocked + needs_review).
    blocked_count:
        Number of rows blocked.
    ready_for_import_count:
        Number of rows classified as ready for import at the bridge
        level.
    issues:
        Pre-built issue breakdown.  When ``None``, an empty
        ``PdfImportRunIssues`` is used.
    parse_warnings:
        Deduplicated, sorted per-row parse warnings.
    extraction_warnings:
        Deduplicated, sorted extraction-level warnings.
    created_at_iso:
        Optional ISO-8601 creation timestamp.

    Returns
    -------
    PdfImportRunReport
        A deterministic, frozen, read-only run report with
        bridge-level counts.
    """
    needs_review_rows = max(0, review_required_count - blocked_count)

    bridge_input = PdfImportRunReportProductionAdapterInput(
        parsed_rows=adapter_input.parsed_rows,
        accepted_rows=adapter_input.accepted_rows,
        imported_rows=matched_count,
        skipped_duplicate_rows=adapter_input.skipped_duplicate_rows,
        idempotent_rows=adapter_input.idempotent_rows,
        blocked_rows=blocked_count,
        needs_review_rows=needs_review_rows,
        ready_for_import_rows=ready_for_import_count,
    )

    return build_pdf_import_run_report_from_production_import_result(
        adapter_input=bridge_input,
        adapter_source=adapter_source,
        batch_public_id=batch_public_id,
        issues=issues,
        parse_warnings=parse_warnings,
        extraction_warnings=extraction_warnings,
        created_at_iso=created_at_iso,
    )


# ---------------------------------------------------------------------------
# Convenience: build from an existing PdfStatementTempDbImportResult
# ---------------------------------------------------------------------------


def build_pdf_import_run_report_production_adapter_from_import_result(
    import_result: Any,
) -> PdfImportRunReport:
    """Convenience wrapper that builds a report from any object matching
    the ``PdfStatementTempDbImportResult`` protocol.

    This is the simplest path for existing fixture-based workflows that
    already have a full ``PdfStatementTempDbImportResult``.  It
    delegates to the existing
    ``build_pdf_import_run_report_from_import_result`` builder.

    Parameters
    ----------
    import_result:
        Any object with the same protocol as
        ``PdfStatementTempDbImportResult``.

    Returns
    -------
    PdfImportRunReport
    """
    from finance_core.reconciliation.pdf_import_run_report import (
        build_pdf_import_run_report_from_import_result,
    )

    return build_pdf_import_run_report_from_import_result(import_result)


# ---------------------------------------------------------------------------
# Convenience: build directly from PdfStatementReviewQueueBridgeResult
# ---------------------------------------------------------------------------


def build_pdf_import_run_report_production_adapter_from_bridge_result(
    bridge_result: Any,
) -> PdfImportRunReport:
    """Convenience wrapper that builds a report from any object matching
    the ``PdfStatementReviewQueueBridgeResult`` protocol.

    Parameters
    ----------
    bridge_result:
        Any object with the same protocol as
        ``PdfStatementReviewQueueBridgeResult``.

    Returns
    -------
    PdfImportRunReport
    """
    from finance_core.reconciliation.pdf_import_run_report import (
        build_pdf_import_run_report_from_bridge_result,
    )

    return build_pdf_import_run_report_from_bridge_result(bridge_result)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PdfImportRunReportProductionAdapterInput",
    "PdfImportRunReportProductionAdapterSource",
    "build_pdf_import_run_report_from_production_import_result",
    "build_pdf_import_run_report_from_production_bridge_result",
    "build_pdf_import_run_report_production_adapter_from_import_result",
    "build_pdf_import_run_report_production_adapter_from_bridge_result",
]
