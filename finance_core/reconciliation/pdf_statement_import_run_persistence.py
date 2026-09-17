"""PDF Statement Import Run Persistence Runtime v1.

Deterministic runtime persistence for PdfStatementImportRunReviewSummary
into the pdf_statement_import_runs table (migration 017).

This module persists import/review run metadata and summary snapshots only.
It does not create final financial transactions, settlement obligations,
parser mutations, Telegram runtime behavior, OCR runtime behavior, or
Metabase runtime/server config.

Key invariants:
- No final financial record mutation.
- SQLite ``UNIQUE(run_public_id)`` is the authoritative replay boundary.
- JSON fields use deterministic ordering (sort_keys=True, compact separators).
- Accepts an explicit sqlite3.Connection; never opens database/finance.db.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from finance_core.persistence_fingerprint import canonical_fingerprint
from finance_core.reconciliation.pdf_statement_import_run_review_summary import (
    PdfStatementImportRunReviewSummary,
    export_run_review_summary_audit_payload,
    export_run_review_summary_dashboard_payload,
)


class PdfStatementImportRunConflictError(ValueError):
    """Same PDF run logical key was submitted with different content."""

    reason_code = "IDEMPOTENCY_KEY_CONTENT_CONFLICT"


class PdfStatementImportRunLegacyFingerprintError(ValueError):
    """A pre-022 row cannot be safely classified without its fingerprint."""

    reason_code = "LEGACY_FINGERPRINT_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Persistence result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfStatementImportRunPersistenceResult:
    """Result of persisting a PdfStatementImportRunReviewSummary.

    Fields
    ------
    run_public_id:
        The run_public_id used for persistence (from summary.run_reference).
    inserted:
        True when a new row was inserted; False when the row already existed.
    already_exists:
        True when the row already existed (idempotent re-persist).
        Always the inverse of ``inserted``.
    row_id:
        The integer primary key of the persisted or existing row.
    run_status:
        The run_status value persisted or retrieved.
    total_rows / ready_for_import_rows / needs_review_rows / blocked_rows:
        Row count values persisted or retrieved.
    """

    run_public_id: str
    inserted: bool
    already_exists: bool
    row_id: int | None
    run_status: str
    total_rows: int
    ready_for_import_rows: int
    needs_review_rows: int
    blocked_rows: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _serialize_reason_counts(
    counts: tuple[tuple[str, int], ...],
) -> str:
    """Serialize blocked/warning reason counts to deterministic JSON."""
    return json.dumps(
        dict(counts) if counts else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _fingerprint_material(summary: PdfStatementImportRunReviewSummary) -> dict[str, object]:
    """Material immutable review fields; evidence and persistence metadata stay out."""
    return {
        "import_batch_public_id": summary.batch_public_id,
        "source_mode": summary.source_mode,
        "source_statement_id": summary.source_statement_id,
        "template_id": summary.template_id,
        "pdf_parsing_mode": summary.pdf_parsing_mode,
        "run_status": summary.run_status,
        "total_rows": summary.total_row_count,
        "ready_for_import_rows": summary.ready_for_import_count,
        "needs_review_rows": summary.needs_review_count,
        "blocked_rows": summary.blocked_count,
        "blocked_reason_counts": list(summary.blocked_reason_counts),
        "warning_reason_counts": list(summary.warning_reason_counts),
        "review_only": summary.review_only,
        "not_final_financial_record": summary.not_final_financial_record,
    }


def _is_run_public_id_conflict(exc: sqlite3.IntegrityError) -> bool:
    return "UNIQUE constraint failed: pdf_statement_import_runs.run_public_id" in str(exc)


def _existing_result(
    conn: sqlite3.Connection, run_public_id: str, fingerprint: str
) -> PdfStatementImportRunPersistenceResult:
    row = conn.execute(
        """SELECT id, content_fingerprint, fingerprint_version, run_status, total_rows,
                  ready_for_import_rows, needs_review_rows, blocked_rows
           FROM pdf_statement_import_runs WHERE run_public_id = ?""",
        (run_public_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("run_public_id conflict did not yield a persisted row")
    if row["content_fingerprint"] is None or row["fingerprint_version"] is None:
        raise PdfStatementImportRunLegacyFingerprintError(
            "Legacy PDF run lacks reconstructable fingerprint material"
        )
    if row["content_fingerprint"] != fingerprint:
        raise PdfStatementImportRunConflictError(
            "PDF import run ID is already bound to different content"
        )
    return PdfStatementImportRunPersistenceResult(
        run_public_id=run_public_id,
        inserted=False,
        already_exists=True,
        row_id=row["id"],
        run_status=row["run_status"],
        total_rows=row["total_rows"],
        ready_for_import_rows=row["ready_for_import_rows"],
        needs_review_rows=row["needs_review_rows"],
        blocked_rows=row["blocked_rows"],
    )


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------


def persist_pdf_statement_import_run_review_summary(
    conn: sqlite3.Connection,
    summary: PdfStatementImportRunReviewSummary,
    *,
    created_at: str | None = None,
) -> PdfStatementImportRunPersistenceResult:
    """Persist a PdfStatementImportRunReviewSummary into pdf_statement_import_runs.

    Idempotent: repeated calls with the same run_public_id will not
    duplicate rows.  The result clearly states whether the row was
    inserted or already existed.

    Parameters
    ----------
    conn:
        An open sqlite3.Connection (must have migration 017 applied).
    summary:
        The review summary to persist.
    created_at:
        Optional creation timestamp.  Uses the DB default
        CURRENT_TIMESTAMP when not supplied.

    Returns
    -------
    PdfStatementImportRunPersistenceResult
    """
    run_public_id = summary.run_reference
    import_batch_public_id = summary.batch_public_id
    source_mode = summary.source_mode
    source_pdf_path = summary.source_pdf_path
    source_statement_id = summary.source_statement_id
    run_status = summary.run_status
    total_rows = summary.total_row_count
    ready_for_import_rows = summary.ready_for_import_count
    needs_review_rows = summary.needs_review_count
    blocked_rows = summary.blocked_count

    blocked_reason_counts_json = _serialize_reason_counts(summary.blocked_reason_counts)
    warning_reason_counts_json = _serialize_reason_counts(summary.warning_reason_counts)
    dashboard_summary_json = json.dumps(
        export_run_review_summary_dashboard_payload(summary),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    audit_summary_json = json.dumps(
        export_run_review_summary_audit_payload(summary),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )

    fingerprint = canonical_fingerprint(
        schema_version="pdf-statement-import-run-v1", material=_fingerprint_material(summary)
    )

    # Read-only optimization; INSERT and its unique-constraint path decide correctness.
    existing = conn.execute(
        "SELECT id FROM pdf_statement_import_runs WHERE run_public_id = ?",
        (run_public_id,),
    ).fetchone()

    if existing is not None:
        return _existing_result(conn, run_public_id, fingerprint)

    try:
        if created_at is not None:
            cursor = conn.execute(
                """\
            INSERT INTO pdf_statement_import_runs (
                run_public_id,
                import_batch_public_id,
                source_mode,
                source_pdf_path,
                source_statement_id,
                run_status,
                total_rows,
                ready_for_import_rows,
                needs_review_rows,
                blocked_rows,
                blocked_reason_counts_json,
                warning_reason_counts_json,
                dashboard_summary_json,
                audit_summary_json,
                content_fingerprint,
                fingerprint_version,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    run_public_id,
                    import_batch_public_id,
                    source_mode,
                    source_pdf_path,
                    source_statement_id,
                    run_status,
                    total_rows,
                    ready_for_import_rows,
                    needs_review_rows,
                    blocked_rows,
                    blocked_reason_counts_json,
                    warning_reason_counts_json,
                    dashboard_summary_json,
                    audit_summary_json,
                    fingerprint,
                    "pdf-statement-import-run-v1",
                    created_at,
                ),
            )
        else:
            cursor = conn.execute(
                """\
            INSERT INTO pdf_statement_import_runs (
                run_public_id,
                import_batch_public_id,
                source_mode,
                source_pdf_path,
                source_statement_id,
                run_status,
                total_rows,
                ready_for_import_rows,
                needs_review_rows,
                blocked_rows,
                blocked_reason_counts_json,
                warning_reason_counts_json,
                dashboard_summary_json,
                audit_summary_json
                , content_fingerprint, fingerprint_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    run_public_id,
                    import_batch_public_id,
                    source_mode,
                    source_pdf_path,
                    source_statement_id,
                    run_status,
                    total_rows,
                    ready_for_import_rows,
                    needs_review_rows,
                    blocked_rows,
                    blocked_reason_counts_json,
                    warning_reason_counts_json,
                    dashboard_summary_json,
                    audit_summary_json,
                    fingerprint,
                    "pdf-statement-import-run-v1",
                ),
            )
    except sqlite3.IntegrityError as exc:
        if not _is_run_public_id_conflict(exc):
            raise
        try:
            return _existing_result(conn, run_public_id, fingerprint)
        except PdfStatementImportRunConflictError as conflict:
            raise conflict from exc

    if cursor.lastrowid is None:
        raise RuntimeError("PDF import run insert did not return a row ID")

    return PdfStatementImportRunPersistenceResult(
        run_public_id=run_public_id,
        inserted=True,
        already_exists=False,
        row_id=cursor.lastrowid,
        run_status=run_status,
        total_rows=total_rows,
        ready_for_import_rows=ready_for_import_rows,
        needs_review_rows=needs_review_rows,
        blocked_rows=blocked_rows,
    )


# ---------------------------------------------------------------------------
# Read helper
# ---------------------------------------------------------------------------


def fetch_pdf_statement_import_run_by_public_id(
    conn: sqlite3.Connection,
    run_public_id: str,
) -> dict[str, object] | None:
    """Fetch a persisted PDF statement import run by run_public_id.

    Parameters
    ----------
    conn:
        An open sqlite3.Connection (must have migration 017 applied).
    run_public_id:
        The run_public_id to look up.

    Returns
    -------
    dict[str, object] | None
        A dict of all columns, or None when no matching row exists.
    """
    row = conn.execute(
        "SELECT * FROM pdf_statement_import_runs WHERE run_public_id = ?",
        (run_public_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PdfStatementImportRunConflictError",
    "PdfStatementImportRunLegacyFingerprintError",
    "PdfStatementImportRunPersistenceResult",
    "fetch_pdf_statement_import_run_by_public_id",
    "persist_pdf_statement_import_run_review_summary",
]
