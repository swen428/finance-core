"""PDF Statement Import Run Persistence Integration v1.

Deterministic integration layer connecting the existing PDF statement
import/review summary builder (#172) to the persistence runtime (#174).

This module wires the full flow end-to-end on a temp/test SQLite database:
  1. Build a PdfStatementImportRunReviewSummary from an import result.
  2. Persist the summary via persist_pdf_statement_import_run_review_summary().
  3. Fetch the persisted row via fetch_pdf_statement_import_run_by_public_id().

Non-goals:
- No production wiring.
- No live database access (database/finance.db).
- No final financial transaction mutation.
- No settlement obligation generation.
- No Telegram / OCR / Metabase runtime changes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from finance_core.reconciliation.pdf_statement_import_run_persistence import (
    PdfStatementImportRunPersistenceResult,
    fetch_pdf_statement_import_run_by_public_id,
    persist_pdf_statement_import_run_review_summary,
)
from finance_core.reconciliation.pdf_statement_import_run_review_summary import (
    build_import_run_review_summary_from_import_result,
)
from finance_core.reconciliation.statement_import import _begin_immediate, _rollback_if_needed

if TYPE_CHECKING:
    from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
        PdfStatementTempDbImportResult,
    )


# ---------------------------------------------------------------------------
# Integration result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfStatementImportRunPersistenceIntegrationResult:
    """Deterministic integration result connecting summary -> persistence -> fetch.

    Fields
    ------
    run_public_id:
        The run_public_id used for persistence.
    inserted:
        True when a new row was inserted; False when the row already existed.
    already_exists:
        True when the row already existed (idempotent re-persist).
    persisted_row_id:
        The integer primary key of the persisted or existing row.
    run_status:
        The run_status value persisted or retrieved.
    total_rows:
        Total row count from the persisted run.
    ready_for_import_rows:
        Ready-for-import count from the persisted run.
    needs_review_rows:
        Needs-review count from the persisted run.
    blocked_rows:
        Blocked count from the persisted run.
    fetched_row_present:
        True when fetch_pdf_statement_import_run_by_public_id() returned
        a non-None row after persistence.
    source_mode:
        The source_mode persisted (from the summary).
    source_pdf_path:
        The source_pdf_path persisted (from the summary).
    source_statement_id:
        The source_statement_id persisted (from the summary).
    dashboard_summary_present:
        True when the fetched row contains a non-null dashboard_summary_json.
    audit_summary_present:
        True when the fetched row contains a non-null audit_summary_json.
    blocked_reason_json_present:
        True when the fetched row contains a non-default
        blocked_reason_counts_json.
    warning_reason_json_present:
        True when the fetched row contains a non-default
        warning_reason_counts_json.
    """

    run_public_id: str
    inserted: bool
    already_exists: bool
    persisted_row_id: int | None
    run_status: str
    total_rows: int
    ready_for_import_rows: int
    needs_review_rows: int
    blocked_rows: int
    fetched_row_present: bool
    source_mode: str
    source_pdf_path: str
    source_statement_id: str
    dashboard_summary_present: bool
    audit_summary_present: bool
    blocked_reason_json_present: bool
    warning_reason_json_present: bool


# ---------------------------------------------------------------------------
# Integration function
# ---------------------------------------------------------------------------


def persist_pdf_statement_import_run_summary_from_import_result(
    conn: sqlite3.Connection,
    import_result: "PdfStatementTempDbImportResult",
    *,
    source_mode: str | None = None,
    created_at: str | None = None,
    _test_post_persist_hook: Callable[[], None] | None = None,
) -> PdfStatementImportRunPersistenceIntegrationResult:
    """Build a run review summary from an import result, persist it, and read back.

    This is a deterministic integration: it connects the existing summary
    builder to the persistence runtime, using only existing functions from
    #172 and #174 without duplicating logic.

    Parameters
    ----------
    conn:
        An open sqlite3.Connection (must have migration 017 applied).
    import_result:
        The result of ``import_pdf_statement_fixture_to_temp_db()``.
    source_mode:
        ``"fixture_text"`` or ``"pdf_text"``.  Passed through to the
        summary builder.  Defaults to the import result's pdf_parsing_mode.
    created_at:
        Optional creation timestamp for the persisted row.

    Returns
    -------
    PdfStatementImportRunPersistenceIntegrationResult
    """
    # Match the authoritative reconciliation import UoW: this service owns a
    # single BEGIN IMMEDIATE / commit-or-rollback boundary and rejects caller
    # transactions rather than committing unrelated work.
    _begin_immediate(conn)
    try:
        summary = build_import_run_review_summary_from_import_result(
            import_result,
            source_mode=source_mode,
        )
        persist_result: PdfStatementImportRunPersistenceResult = (
            persist_pdf_statement_import_run_review_summary(conn, summary, created_at=created_at)
        )
        if _test_post_persist_hook is not None:
            _test_post_persist_hook()
        fetched = fetch_pdf_statement_import_run_by_public_id(conn, persist_result.run_public_id)
        if fetched is None:
            raise RuntimeError("Persisted PDF import run could not be read back")

        result = PdfStatementImportRunPersistenceIntegrationResult(
            run_public_id=persist_result.run_public_id,
            inserted=persist_result.inserted,
            already_exists=persist_result.already_exists,
            persisted_row_id=persist_result.row_id,
            run_status=persist_result.run_status,
            total_rows=persist_result.total_rows,
            ready_for_import_rows=persist_result.ready_for_import_rows,
            needs_review_rows=persist_result.needs_review_rows,
            blocked_rows=persist_result.blocked_rows,
            fetched_row_present=True,
            source_mode=summary.source_mode,
            source_pdf_path=summary.source_pdf_path,
            source_statement_id=summary.source_statement_id,
            dashboard_summary_present=fetched.get("dashboard_summary_json") is not None,
            audit_summary_present=fetched.get("audit_summary_json") is not None,
            blocked_reason_json_present=(
                fetched.get("blocked_reason_counts_json") not in (None, "{}")
            ),
            warning_reason_json_present=(
                fetched.get("warning_reason_counts_json") not in (None, "{}")
            ),
        )
        conn.commit()
        return result
    except Exception:
        _rollback_if_needed(conn)
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PdfStatementImportRunPersistenceIntegrationResult",
    "persist_pdf_statement_import_run_summary_from_import_result",
]
