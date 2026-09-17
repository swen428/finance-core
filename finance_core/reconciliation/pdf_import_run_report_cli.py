"""PDF Import Run Report CLI / Workflow Output v1.

Lightweight deterministic CLI/report fixture that runs the existing
PDF statement fixture import/review flow and produces a
``PdfImportRunReport`` with selectable output modes.

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

import argparse
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from finance_core.reconciliation.pdf_import_run_report import (
    PdfImportRunReport,
    build_pdf_import_run_report_from_import_result,
    export_pdf_import_run_report_audit_payload,
    export_pdf_import_run_report_dashboard_payload,
    format_pdf_import_run_report_text,
)
from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
    DEFAULT_BATCH_PUBLIC_ID,
    DEFAULT_PDF_FIXTURE_PATH,
    DEFAULT_TEMPLATE_ID,
    DEFAULT_TEXT_FIXTURE_PATH,
    import_pdf_statement_fixture_to_temp_db,
)

# ---------------------------------------------------------------------------
# CLI result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfImportRunReportCliResult:
    """Frozen CLI result wrapping a ``PdfImportRunReport`` with output metadata.

    Fields
    ------
    report:
        The deterministic ``PdfImportRunReport`` built from the fixture
        import result.
    source_mode:
        ``"fixture_text"`` (deterministic CI) or ``"pdf_text"``
        (real PDF text extraction).
    output_mode:
        ``"text"``, ``"dashboard-json"``, or ``"audit-json"``.
    output_text:
        Pre-rendered output for the requested mode.
    """

    report: PdfImportRunReport
    source_mode: str
    output_mode: str
    output_text: str


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def _render_text_output(report: PdfImportRunReport) -> str:
    """Render the report as deterministic human-readable text."""
    return format_pdf_import_run_report_text(report)


def _render_dashboard_json(report: PdfImportRunReport) -> str:
    """Render the report as a dashboard-safe JSON payload."""
    payload = export_pdf_import_run_report_dashboard_payload(report)
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


def _render_audit_json(report: PdfImportRunReport) -> str:
    """Render the report as an audit JSON payload with full source evidence."""
    payload = export_pdf_import_run_report_audit_payload(report)
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


_OUTPUT_RENDERERS = {
    "text": _render_text_output,
    "dashboard-json": _render_dashboard_json,
    "audit-json": _render_audit_json,
}


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the PDF Import Run Report CLI.

    Returns an ``ArgumentParser`` configured with the supported output
    modes, source modes, and optional db-path.  The caller must call
    ``parser.parse_args()`` to produce the namespace.
    """
    parser = argparse.ArgumentParser(
        description="PDF Import Run Report CLI -- fixture-only run report output (v1)",
    )
    parser.add_argument(
        "--output",
        choices=["text", "dashboard-json", "audit-json"],
        default="text",
        help="Output mode.  'text' prints a human-readable run report. "
        "'dashboard-json' exports a dashboard-safe JSON payload (no source "
        "evidence).  'audit-json' exports a full audit JSON payload "
        "including source evidence.  Defaults to 'text'.",
    )
    parser.add_argument(
        "--source-mode",
        choices=["fixture_text", "pdf_text"],
        default="fixture_text",
        help="Source mode for the fixture import.  'fixture_text' uses the "
        "deterministic text fixture (default, CI-safe).  'pdf_text' uses "
        "real PDF text extraction from the fixture PDF file.",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Optional path for the import temp DB.  When omitted, a "
        "temporary directory is created automatically and cleaned up.  "
        "Must not be the live database.",
    )
    return parser


# ---------------------------------------------------------------------------
# Fixture runner
# ---------------------------------------------------------------------------


def run_pdf_import_run_report_fixture(
    *,
    output: str = "text",
    source_mode: str = "fixture_text",
    db_path: str | None = None,
) -> PdfImportRunReportCliResult:
    """Run the full PDF statement import fixture flow and produce a run report.

    Parameters
    ----------
    output:
        ``"text"`` (default), ``"dashboard-json"``, or ``"audit-json"``.
    source_mode:
        ``"fixture_text"`` (default) or ``"pdf_text"``.
    db_path:
        Optional path for the import temp DB.  When omitted, a temporary
        directory is created automatically and cleaned up.

    Returns
    -------
    PdfImportRunReportCliResult

    Raises
    ------
    ValueError
        If ``output`` is not one of the supported modes.
    """
    if output not in _OUTPUT_RENDERERS:
        raise ValueError(
            f"Unsupported output mode {output!r}; "
            f"expected one of: {', '.join(sorted(_OUTPUT_RENDERERS))}"
        )

    if db_path is None:
        tmpdir = tempfile.TemporaryDirectory()
        import_db_path = str(Path(tmpdir.name) / "import_fixture.db")
    else:
        tmpdir = None
        import_db_path = db_path

    try:
        import_result = import_pdf_statement_fixture_to_temp_db(
            db_path=str(import_db_path),
            pdf_path=str(DEFAULT_PDF_FIXTURE_PATH),
            text_fixture_path=str(DEFAULT_TEXT_FIXTURE_PATH),
            template_id=DEFAULT_TEMPLATE_ID,
            batch_public_id=DEFAULT_BATCH_PUBLIC_ID,
            source_mode=source_mode,
        )
        report = build_pdf_import_run_report_from_import_result(import_result)

        effective_source_mode = import_result.pdf_parsing_mode
        render_fn = _OUTPUT_RENDERERS[output]
        rendered = render_fn(report)

        return PdfImportRunReportCliResult(
            report=report,
            source_mode=effective_source_mode,
            output_mode=output,
            output_text=rendered,
        )
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()


def main(argv: list[str] | None = None) -> int:
    """Run the PDF Import Run Report CLI from parsed arguments.

    Returns 0 on success.
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    result = run_pdf_import_run_report_fixture(
        output=args.output,
        source_mode=args.source_mode,
        db_path=args.db_path,
    )
    print(result.output_text)
    return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "build_arg_parser",
    "main",
    "PdfImportRunReportCliResult",
    "run_pdf_import_run_report_fixture",
]

if __name__ == "__main__":
    raise SystemExit(main())
