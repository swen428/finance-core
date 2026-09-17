"""PDF Statement Import Run Review CLI / Report Output v1.

Lightweight deterministic CLI/report fixture that runs the existing
PDF statement fixture import/review flow, optionally persists the run
summary into a temp SQLite DB, and produces a human-readable review
report.

This is not production wiring.

Non-goals:

- No raw PDF parsing.
- No OCR.
- No database connection lifecycle management beyond temp DB.
- No mutation of final financial records.
- No settlement obligation generation.
- No AI / model calls.
- No Telegram runtime changes.
- No Metabase runtime / server config changes.

Safety:

- Temp DB / fixture DB only; never touches database/finance.db.
- Review-only guard flags are always True.
- Does not write final transactions.
- Does not generate settlement obligations.
"""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from finance_core.reconciliation.migrations import apply_migration_path
from finance_core.reconciliation.pdf_statement_import_run_persistence_integration import (
    persist_pdf_statement_import_run_summary_from_import_result,
)
from finance_core.reconciliation.pdf_statement_import_run_review_summary import (
    build_import_run_review_summary_from_import_result,
)
from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
    DEFAULT_BATCH_PUBLIC_ID,
    DEFAULT_PDF_FIXTURE_PATH,
    DEFAULT_TEMPLATE_ID,
    DEFAULT_TEXT_FIXTURE_PATH,
    PdfStatementTempDbImportResult,
    import_pdf_statement_fixture_to_temp_db,
)
from finance_core.resources import migrations_dir
from finance_core.runtime_paths import live_database_path
from finance_core.sqlite_connection import ConnectionMode, connect_sqlite

if TYPE_CHECKING:
    from finance_core.reconciliation.pdf_statement_import_run_persistence_integration import (
        PdfStatementImportRunPersistenceIntegrationResult,
    )

# ---------------------------------------------------------------------------
# Migration path for persistence
# ---------------------------------------------------------------------------

_MIGRATION_017_PATH = migrations_dir() / "017_pdf_statement_import_run_persistence.sql"
_MIGRATION_022_PATH = migrations_dir() / "022_database_conflict_fingerprints.sql"


def _apply_persistence_migrations(conn: sqlite3.Connection) -> None:
    """Apply PDF persistence migrations 017 and 022 at the migration boundary."""
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    if "pdf_statement_import_runs" not in tables:
        apply_migration_path(conn, _MIGRATION_017_PATH)
    apply_migration_path(conn, _MIGRATION_022_PATH)


# ---------------------------------------------------------------------------
# CLI result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfStatementImportRunReviewCliResult:
    """Frozen result from the PDF statement import run review CLI/report.

    Fields
    ------
    run_public_id:
        Public run identifier from the review summary.
    batch_public_id:
        Public batch identifier from the import batch.
    source_mode:
        ``"fixture_text"`` (deterministic CI) or ``"pdf_text"``
        (real PDF text extraction).
    run_status:
        ``"fully_ready"``, ``"partially_reviewable"``, or ``"blocked"``.
    total_rows:
        Total number of rows in the review queue.
    ready_for_import_rows:
        Number of rows classified as ready_for_import.
    needs_review_rows:
        Number of rows classified as needs_review.
    blocked_rows:
        Number of rows classified as blocked.
    blocked_reason_counts:
        Deterministic sorted tuple of (reason_str, count) for every
        blocked reason seen.
    warning_reason_counts:
        Deterministic sorted tuple of (warning_text, count) for every
        distinct warning message.

    Persistence (present only when persistence is enabled):
    persistence_enabled:
        Whether persistence was enabled for this run.
    inserted:
        True when a new row was inserted; None when persistence disabled.
    already_exists:
        True when the row already existed; None when persistence disabled.
    persisted_row_id:
        The integer primary key of the persisted or existing row; None when
        persistence disabled.
    fetched_row_present:
        True when fetch returned a non-None row after persistence; None when
        persistence disabled.

    Guard flags:
    dashboard_safe:
        Always True -- dashboard-safe summary marker.
    audit_evidence_available:
        Always True -- audit/evidence marker.
    review_only:
        Always True.
    not_final_financial_record:
        Always True.
    """

    # Run identity
    run_public_id: str
    batch_public_id: str

    # Source info
    source_mode: str

    # Row classification
    run_status: str
    total_rows: int
    ready_for_import_rows: int
    needs_review_rows: int
    blocked_rows: int

    # Reason breakdowns
    blocked_reason_counts: tuple[tuple[str, int], ...]
    warning_reason_counts: tuple[tuple[str, int], ...]

    # Persistence (all None when not enabled)
    persistence_enabled: bool = False
    inserted: bool | None = None
    already_exists: bool | None = None
    persisted_row_id: int | None = None
    fetched_row_present: bool | None = None

    # Guard flags
    dashboard_safe: bool = True
    audit_evidence_available: bool = True
    review_only: bool = True
    not_final_financial_record: bool = True


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def build_pdf_statement_import_run_review_report(
    import_result: PdfStatementTempDbImportResult,
    *,
    source_mode: str | None = None,
    enable_persistence: bool = False,
    persistence_db_path: str | None = None,
    created_at: str | None = None,
) -> PdfStatementImportRunReviewCliResult:
    """Build a review report from a deterministic import result.

    Parameters
    ----------
    import_result:
        The result of ``import_pdf_statement_fixture_to_temp_db()``.
    source_mode:
        ``"fixture_text"`` or ``"pdf_text"``.  Defaults to the
        ``pdf_parsing_mode`` from the import result.
    enable_persistence:
        When True, persists the run summary into a temp SQLite DB
        using the existing integration from PR #175 and includes
        persistence fields in the result.
    persistence_db_path:
        Path to the persistence temp DB.  Required when
        ``enable_persistence`` is True.  Must not be
        ``database/finance.db``.
    created_at:
        Optional deterministic timestamp for persistence.

    Returns
    -------
    PdfStatementImportRunReviewCliResult

    Raises
    ------
    ValueError
        If ``enable_persistence`` is True but ``persistence_db_path``
        is not provided or points to ``database/finance.db``.
    """
    # Build the summary
    effective_source_mode = source_mode or import_result.pdf_parsing_mode
    summary = build_import_run_review_summary_from_import_result(
        import_result,
        source_mode=effective_source_mode,
    )

    # Persistence (optional)
    insert_flag: bool | None = None
    already_exists_flag: bool | None = None
    row_id: int | None = None
    fetch_present: bool | None = None

    if enable_persistence:
        if persistence_db_path is None:
            raise ValueError("persistence_db_path is required when enable_persistence is True")
        resolved_db = Path(persistence_db_path).expanduser().resolve()
        live_db = live_database_path()
        if resolved_db == live_db:
            raise ValueError(
                "Refusing to use database/finance.db for PDF statement "
                "import run review persistence"
            )

        conn = connect_sqlite(resolved_db, mode=ConnectionMode.APPLICATION)
        try:
            _apply_persistence_migrations(conn)
            persist_result: PdfStatementImportRunPersistenceIntegrationResult = (
                persist_pdf_statement_import_run_summary_from_import_result(
                    conn,
                    import_result,
                    source_mode=effective_source_mode,
                    created_at=created_at,
                )
            )
            insert_flag = persist_result.inserted
            already_exists_flag = persist_result.already_exists
            row_id = persist_result.persisted_row_id
            fetch_present = persist_result.fetched_row_present
        finally:
            conn.close()

    return PdfStatementImportRunReviewCliResult(
        run_public_id=summary.run_reference,
        batch_public_id=summary.batch_public_id,
        source_mode=effective_source_mode,
        run_status=summary.run_status,
        total_rows=summary.total_row_count,
        ready_for_import_rows=summary.ready_for_import_count,
        needs_review_rows=summary.needs_review_count,
        blocked_rows=summary.blocked_count,
        blocked_reason_counts=summary.blocked_reason_counts,
        warning_reason_counts=summary.warning_reason_counts,
        persistence_enabled=enable_persistence,
        inserted=insert_flag,
        already_exists=already_exists_flag,
        persisted_row_id=row_id,
        fetched_row_present=fetch_present,
        dashboard_safe=True,
        audit_evidence_available=True,
        review_only=True,
        not_final_financial_record=True,
    )


# ---------------------------------------------------------------------------
# Fixture runner
# ---------------------------------------------------------------------------


def run_pdf_statement_import_run_review_fixture(
    *,
    db_path: str | None = None,
    enable_persistence: bool = False,
    source_mode: str = "fixture_text",
    created_at: str | None = None,
) -> PdfStatementImportRunReviewCliResult:
    """Run the full PDF statement import/review fixture and produce a report.

    Parameters
    ----------
    db_path:
        Optional path for the import temp DB.  When omitted, a temporary
        directory is created automatically and cleaned up.
    enable_persistence:
        When True, persists the run summary into a separate temp SQLite DB
        using the existing integration from PR #175.
    source_mode:
        ``"fixture_text"`` (default) or ``"pdf_text"``.
    created_at:
        Optional deterministic timestamp for persistence.

    Returns
    -------
    PdfStatementImportRunReviewCliResult
    """
    if db_path is None:
        tmpdir = tempfile.TemporaryDirectory()
        import_db_path = str(Path(tmpdir.name) / "import_fixture.db")
    else:
        tmpdir = None
        import_db_path = db_path

    persistence_db_path: str | None = None
    if enable_persistence:
        if db_path is None:
            # db_path is None implies tmpdir was set in the block above, but
            # mypy cannot propagate that conditional across the nesting gap.
            assert tmpdir is not None
            persistence_db_path = str(Path(tmpdir.name) / "persistence.db")
        else:
            persistence_db_path = str(Path(db_path).parent / "cli_persistence.db")

    try:
        import_result = import_pdf_statement_fixture_to_temp_db(
            db_path=str(import_db_path),
            pdf_path=str(DEFAULT_PDF_FIXTURE_PATH),
            text_fixture_path=str(DEFAULT_TEXT_FIXTURE_PATH),
            template_id=DEFAULT_TEMPLATE_ID,
            batch_public_id=DEFAULT_BATCH_PUBLIC_ID,
            source_mode=source_mode,
        )
        return build_pdf_statement_import_run_review_report(
            import_result,
            source_mode=source_mode,
            enable_persistence=enable_persistence,
            persistence_db_path=persistence_db_path,
            created_at=created_at,
        )
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()


# ---------------------------------------------------------------------------
# Text formatter
# ---------------------------------------------------------------------------


def format_pdf_statement_import_run_review_report(
    result: PdfStatementImportRunReviewCliResult,
) -> str:
    """Format the review report as stable, human-readable plain text.

    Output includes run identity, source mode, row counts, reason
    breakdowns, persistence fields (when enabled), and guard flags.
    Suitable for terminal display, log output, or text-file snapshots.
    """
    lines: list[str] = []
    lines.append("PDF Statement Import Run Review")
    lines.append("")
    lines.append(f"run_public_id: {result.run_public_id}")
    lines.append(f"batch_public_id: {result.batch_public_id}")
    lines.append(f"source_mode: {result.source_mode}")
    lines.append(f"run_status: {result.run_status}")
    lines.append("")
    lines.append("rows:")
    lines.append(f"  total: {result.total_rows}")
    lines.append(f"  ready_for_import: {result.ready_for_import_rows}")
    lines.append(f"  needs_review: {result.needs_review_rows}")
    lines.append(f"  blocked: {result.blocked_rows}")

    if result.blocked_reason_counts:
        lines.append("")
        lines.append("blocked_reasons:")
        for reason, count in result.blocked_reason_counts:
            lines.append(f"  {reason}: {count}")
    else:
        lines.append("")
        lines.append("blocked_reasons:")
        lines.append("  none: 0")

    if result.warning_reason_counts:
        lines.append("")
        lines.append("warning_reasons:")
        for warning, count in result.warning_reason_counts:
            lines.append(f'  "{warning}": {count}')
    else:
        lines.append("")
        lines.append("warning_reasons:")
        lines.append("  (none)")

    lines.append("")
    lines.append("persistence:")
    lines.append(f"  enabled: {str(result.persistence_enabled).lower()}")
    if result.persistence_enabled:
        lines.append(f"  inserted: {str(result.inserted).lower()}")
        lines.append(f"  already_exists: {str(result.already_exists).lower()}")
        lines.append(f"  persisted_row_id: {result.persisted_row_id}")
        lines.append(f"  fetched_row_present: {str(result.fetched_row_present).lower()}")

    lines.append("")
    lines.append("dashboard:")
    lines.append(f"  review_only: {str(result.review_only).lower()}")
    lines.append(f"  dashboard_safe: {str(result.dashboard_safe).lower()}")
    lines.append("")
    lines.append("audit:")
    lines.append(f"  evidence_available: {str(result.audit_evidence_available).lower()}")
    lines.append(f"  not_final_financial_record: {str(result.not_final_financial_record).lower()}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PdfStatementImportRunReviewCliResult",
    "build_pdf_statement_import_run_review_report",
    "format_pdf_statement_import_run_review_report",
    "run_pdf_statement_import_run_review_fixture",
]
