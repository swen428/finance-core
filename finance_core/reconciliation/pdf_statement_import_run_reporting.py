"""PDF Statement Import Run Reporting Query Pack v1.

Read-only reporting query helpers for pdf_statement_import_runs
(migration 017).  Every helper is side-effect-free: it opens a
connection, executes SELECT queries, and closes the connection.
No INSERT, UPDATE, DELETE, or schema mutation is performed.

Non-goals:

- No PDF parsing.
- No OCR / AI / model calls.
- No final financial record mutation.
- No settlement obligation generation.
- No Telegram runtime wiring.
- No production Metabase config.
- No writes to database/finance.db or live data.
- No migration creation or schema changes.

Safety:

- Read-only by construction -- all queries start with SELECT.
- Dashboard-safe output excludes audit/evidence fields.
- Audit fields are documented separately for clarity.
- Deterministic ordering: created_at DESC, run_public_id ASC.
- Accepts an explicit SQLite database path; never opens
  database/finance.db on its own.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from finance_core.sqlite_connection import ConnectionMode, connect_sqlite

# ---------------------------------------------------------------------------
# Dashboard-safe row dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfImportRunDashboardRow:
    """A single row suitable for dashboard / report display.

    Excludes audit trail fields (source_pdf_path, source_statement_id),
    internal JSON snapshots (blocked_reason_counts_json,
    warning_reason_counts_json, dashboard_summary_json,
    audit_summary_json), and internal primary key (id).
    """

    run_public_id: str
    import_batch_public_id: str | None
    source_mode: str
    run_status: str
    total_rows: int
    ready_for_import_rows: int
    needs_review_rows: int
    blocked_rows: int
    created_at: str

    def to_dict(self) -> dict[str, object]:
        """Return a dict representation suitable for JSON serialization."""
        return {
            "run_public_id": self.run_public_id,
            "import_batch_public_id": self.import_batch_public_id,
            "source_mode": self.source_mode,
            "run_status": self.run_status,
            "total_rows": self.total_rows,
            "ready_for_import_rows": self.ready_for_import_rows,
            "needs_review_rows": self.needs_review_rows,
            "blocked_rows": self.blocked_rows,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Reporting query pack
# ---------------------------------------------------------------------------


@dataclass
class PdfImportRunReportingQueryPack:
    """Read-only reporting query pack for pdf_statement_import_runs.

    All methods accept a database path, open a read-only connection, execute
    SELECT-only queries, and return deterministic results.  No write operations
    are ever performed.

    ClassVar constants
    ------------------
    DASHBOARD_SELECT_SQL:
        Stable SQL SELECT projecting only dashboard-safe columns.
    DASHBOARD_FIELD_NAMES:
        Frozen tuple of dashboard-safe column name strings, in order.
    AUDIT_ONLY_FIELD_NAMES:
        Frozen tuple of audit-only column name strings that are deliberately
        excluded from dashboard output.

    Ordering guarantee
    ------------------
    All listing methods return rows ordered by ``created_at DESC,
    run_public_id ASC`` (newest first, stable within same timestamp).
    """

    # -- SQL and field constants --

    DASHBOARD_SELECT_SQL: ClassVar[str] = (
        "SELECT run_public_id, import_batch_public_id, source_mode, "
        "run_status, total_rows, ready_for_import_rows, "
        "needs_review_rows, blocked_rows, created_at "
        "FROM pdf_statement_import_runs "
        "ORDER BY created_at DESC, run_public_id ASC"
    )
    """Stable read-only SELECT projecting all dashboard-safe columns.

    Ordering: newest ``created_at`` first, with ``run_public_id ASC``
    as a deterministic tie-breaker when two rows share the same timestamp.
    """

    DASHBOARD_FIELD_NAMES: ClassVar[tuple[str, ...]] = (
        "run_public_id",
        "import_batch_public_id",
        "source_mode",
        "run_status",
        "total_rows",
        "ready_for_import_rows",
        "needs_review_rows",
        "blocked_rows",
        "created_at",
    )
    """Dashboard-safe field names in the order they appear in the SELECT."""

    AUDIT_ONLY_FIELD_NAMES: ClassVar[tuple[str, ...]] = (
        "source_pdf_path",
        "source_statement_id",
        "blocked_reason_counts_json",
        "warning_reason_counts_json",
        "dashboard_summary_json",
        "audit_summary_json",
    )
    """Audit-only field names deliberately excluded from dashboard output.

    These columns exist in the schema (migration 017) but are never
    projected by the dashboard-safe SELECT.  They may be retrieved
    through the lower-level ``PdfImportRunPersistence`` module or via
    direct SQL for audit traceability.
    """

    # Signature explicitly unused to make reporting-only intent clear
    _reporting_only: ClassVar[bool] = True

    # ------------------------------------------------------------------
    # Dashboard listing
    # ------------------------------------------------------------------

    @staticmethod
    def list_dashboard_rows(db_path: str | Path) -> list[PdfImportRunDashboardRow]:
        """Return all runs as dashboard-safe rows, newest first.

        Parameters
        ----------
        db_path:
            Path to a SQLite database with migration 017 applied.

        Returns
        -------
        list[PdfImportRunDashboardRow]
            Stable, sorted list of dashboard-safe rows.  Empty when
            the table has no rows.
        """
        with contextlib.closing(connect_sqlite(db_path, mode=ConnectionMode.READ_ONLY)) as conn:
            rows = conn.execute(PdfImportRunReportingQueryPack.DASHBOARD_SELECT_SQL).fetchall()
            return [
                PdfImportRunDashboardRow(
                    run_public_id=row["run_public_id"],
                    import_batch_public_id=row["import_batch_public_id"],
                    source_mode=row["source_mode"],
                    run_status=row["run_status"],
                    total_rows=row["total_rows"],
                    ready_for_import_rows=row["ready_for_import_rows"],
                    needs_review_rows=row["needs_review_rows"],
                    blocked_rows=row["blocked_rows"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    @staticmethod
    def list_dashboard_rows_as_dicts(
        db_path: str | Path,
    ) -> list[dict[str, object]]:
        """Return all runs as dashboard-safe plain dicts, newest first.

        Convenience wrapper that avoids importing the dataclass.  Each
        dict contains exactly the keys in
        ``PdfImportRunReportingQueryPack.DASHBOARD_FIELD_NAMES``.

        Parameters
        ----------
        db_path:
            Path to a SQLite database with migration 017 applied.

        Returns
        -------
        list[dict[str, object]]
            Stable, sorted list of dashboard-safe dicts.  Empty when
            the table has no rows.
        """
        rows = PdfImportRunReportingQueryPack.list_dashboard_rows(db_path)
        return [r.to_dict() for r in rows]

    # ------------------------------------------------------------------
    # Status aggregation
    # ------------------------------------------------------------------

    @staticmethod
    def count_by_status(db_path: str | Path) -> dict[str, int]:
        """Return a mapping of run_status to run count.

        Only statuses that have at least one run are included.
        Returns an empty dict when the table has no rows.
        """
        with contextlib.closing(connect_sqlite(db_path, mode=ConnectionMode.READ_ONLY)) as conn:
            rows = conn.execute(
                "SELECT run_status, COUNT(*) AS cnt "
                "FROM pdf_statement_import_runs "
                "GROUP BY run_status "
                "ORDER BY run_status"
            ).fetchall()
            return {row["run_status"]: row["cnt"] for row in rows}

    @staticmethod
    def count_by_source_mode(db_path: str | Path) -> dict[str, int]:
        """Return a mapping of source_mode to run count.

        Only source modes that have at least one run are included.
        Returns an empty dict when the table has no rows.
        """
        with contextlib.closing(connect_sqlite(db_path, mode=ConnectionMode.READ_ONLY)) as conn:
            rows = conn.execute(
                "SELECT source_mode, COUNT(*) AS cnt "
                "FROM pdf_statement_import_runs "
                "GROUP BY source_mode "
                "ORDER BY source_mode"
            ).fetchall()
            return {row["source_mode"]: row["cnt"] for row in rows}


# ---------------------------------------------------------------------------
# Convenience functions (module level)
# ---------------------------------------------------------------------------


def list_pdf_import_run_dashboard_rows(
    db_path: str | Path,
) -> list[PdfImportRunDashboardRow]:
    """Convenience alias for PdfImportRunReportingQueryPack.list_dashboard_rows."""
    return PdfImportRunReportingQueryPack.list_dashboard_rows(db_path)


def list_pdf_import_run_dashboard_rows_as_dicts(
    db_path: str | Path,
) -> list[dict[str, object]]:
    """Convenience alias for PdfImportRunReportingQueryPack.list_dashboard_rows_as_dicts."""
    return PdfImportRunReportingQueryPack.list_dashboard_rows_as_dicts(db_path)


def count_pdf_import_runs_by_status(
    db_path: str | Path,
) -> dict[str, int]:
    """Convenience alias for PdfImportRunReportingQueryPack.count_by_status."""
    return PdfImportRunReportingQueryPack.count_by_status(db_path)


def count_pdf_import_runs_by_source_mode(
    db_path: str | Path,
) -> dict[str, int]:
    """Convenience alias for PdfImportRunReportingQueryPack.count_by_source_mode."""
    return PdfImportRunReportingQueryPack.count_by_source_mode(db_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PdfImportRunDashboardRow",
    "PdfImportRunReportingQueryPack",
    "list_pdf_import_run_dashboard_rows",
    "list_pdf_import_run_dashboard_rows_as_dicts",
    "count_pdf_import_runs_by_status",
    "count_pdf_import_runs_by_source_mode",
]
