"""PDF Statement Review Queue Demo CLI v1.

Operator-facing demo CLI that runs the PDF statement review queue bridge
end-to-end in a temporary database only.  Supports two source modes:

- ``fixture-text`` (default, deterministic CI): uses a checked-in
  extracted-text fixture.
- ``pdf-text``: extracts text from a real text-based PDF via the
  ``pdf_text_extraction_adapter``, then feeds the text through the
  template parser.

Usage::

    # Fixture-text mode (default)
    python -m finance_core.reconciliation.pdf_statement_review_queue_demo_cli \\
        --db /tmp/pdf_review_demo.sqlite

    # PDF-text mode
    python -m finance_core.reconciliation.pdf_statement_review_queue_demo_cli \\
        --source-mode pdf-text --pdf-path /path/to/statement.pdf \\
        --db /tmp/pdf_review_demo.sqlite

Non-goals:

- No production OCR or direct PDF parsing (delegated to the adapter).
- No database/finance.db access (always refused).
- No final financial record mutation.
- No settlement obligation generation.
- No AI/model calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from finance_core.reconciliation.migrations import LIVE_DB_PATH
from finance_core.reconciliation.pdf_statement_review_queue_bridge import (
    PdfStatementReviewQueueBridgeResult,
    bridge_result_to_summary_dict,
    run_pdf_statement_review_queue_bridge,
)
from finance_core.reconciliation.pdf_statement_review_queue_fixture import (
    format_pdf_statement_review_queue_text,
)
from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
    DEFAULT_PDF_FIXTURE_PATH,
    DEFAULT_TEMPLATE_ID,
    DEFAULT_TEXT_FIXTURE_PATH,
    result_to_summary_dict,
)

_LIVE_DB_PATH = LIVE_DB_PATH.resolve()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the PDF statement review queue demo CLI.  Returns 0 on success."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 1
    return _run_demo_command(args)


def _run_demo_command(args: argparse.Namespace) -> int:
    """Validate arguments and run the bridge."""
    try:
        _guard_db_path(args.db)

        # Guard :memory: in persistence mode
        if args.persist_review_queue:
            if str(args.db) == ":memory:":
                print(
                    "Error: --persist-review-queue cannot be used with :memory: database. "
                    "Use a file-based temp DB.",
                    file=sys.stderr,
                )
                return 1

        source_mode = args.source_mode.replace("-", "_")
        if source_mode not in ("fixture_text", "pdf_text"):
            print(
                f"Error: unsupported source-mode '{args.source_mode}'. "
                "Use 'fixture-text' or 'pdf-text'.",
                file=sys.stderr,
            )
            return 1

        if source_mode == "pdf_text":
            if args.pdf_path is None:
                print(
                    "Error: --pdf-path is required when --source-mode pdf-text.",
                    file=sys.stderr,
                )
                return 1
            pdf_path = Path(args.pdf_path).expanduser().resolve()
            if not pdf_path.exists():
                print(f"Error: PDF file not found: {pdf_path}", file=sys.stderr)
                return 1
            if pdf_path.suffix.lower() != ".pdf":
                print(f"Error: --pdf-path must point to a .pdf file: {pdf_path}", file=sys.stderr)
                return 1
        else:
            pdf_path = Path(args.pdf_path) if args.pdf_path else DEFAULT_PDF_FIXTURE_PATH

        persistence_run_id = args.persistence_run_id or None
        result = run_pdf_statement_review_queue_bridge(
            db_path=args.db,
            source_mode=source_mode,
            pdf_path=pdf_path,
            text_fixture_path=args.text_fixture or DEFAULT_TEXT_FIXTURE_PATH,
            template_id=args.template or DEFAULT_TEMPLATE_ID,
            batch_public_id=args.batch_id or None,
            persist_review_queue=args.persist_review_queue,
            persistence_run_public_id=persistence_run_id,
        )

        if args.json:
            _print_json_output(result)
        else:
            _print_text_output(result)

        return 0

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _print_json_output(result: PdfStatementReviewQueueBridgeResult) -> None:
    """Emit the bridge result as a compact JSON document."""
    summary = bridge_result_to_summary_dict(result)
    print(json.dumps(summary, indent=2, sort_keys=True))


def _print_text_output(result: PdfStatementReviewQueueBridgeResult) -> None:
    """Emit a human-readable summary of the bridge result."""
    import_summary = result_to_summary_dict(result.import_result)

    print("=" * 64)
    print("PDF Statement Review Queue Demo")
    print("=" * 64)
    print()
    print(f"Source mode:         {result.source_mode}")
    print(f"PDF parsing mode:    {result.pdf_parsing_mode}")
    print(f"PDF path:            {import_summary['pdf_path']}")
    if result.source_mode == "fixture_text":
        print(f"Text fixture path:   {import_summary['text_fixture_path']}")
    print(f"Temp DB path:        {import_summary['db_path']}")
    print(f"Template:            {import_summary['template_id']}")
    print(f"Source type:         {import_summary['source_type']}")
    if result.persisted_count > 0:
        print(f"Review queue mode:   persisted ({result.persisted_count} rows)")
        if result.persistence_run_public_id:
            print(f"Persistence run ID:  {result.persistence_run_public_id}")
    else:
        print("Review queue mode:   in-memory (not persisted)")
    print()
    print(f"Total rows:          {result.review_queue.summary.total_rows}")
    print(f"  Matched (imported):    {result.matched_count}")
    print(f"  Ready for import:      {result.ready_for_import_count}")
    print(f"  Needs review:          {result.review_queue.summary.needs_review_count}")
    print(f"  Blocked:               {result.blocked_count}")
    print(f"  Review required:       {result.review_required_count}")
    print(f"  Warnings:              {result.review_queue.summary.warnings_count}")
    print()
    print("review_only=True  not_final_financial_record=True")
    print()

    # Print the review queue text (review items)
    review_text = format_pdf_statement_review_queue_text(result.review_queue)
    print(review_text)


def _guard_db_path(db_arg: str | None) -> None:
    """Fail if --db is omitted or points to database/finance.db."""
    if db_arg is None:
        raise ValueError("Error: --db is required. The CLI never defaults to database/finance.db.")
    if str(db_arg) == ":memory:":
        raise ValueError("Refusing to use :memory: for PDF review queue demo CLI.")
    resolved = Path(db_arg).expanduser().resolve()
    if resolved == _LIVE_DB_PATH:
        raise ValueError("Refusing to use database/finance.db for PDF review queue demo CLI.")
    if resolved.name == "finance.db" and resolved.parent == _LIVE_DB_PATH.parent:
        raise ValueError("Refusing to use database/finance.db for PDF review queue demo CLI.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the PDF statement review queue demo in a temp DB. "
            "Uses deterministic fixture text or real PDF text extraction. "
            "This is a fixture/demo command, NOT production OCR/PDF parsing."
        ),
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to a temporary SQLite database file (never database/finance.db).",
    )
    parser.add_argument(
        "--source-mode",
        choices=["fixture-text", "pdf-text"],
        default="fixture-text",
        help="Source mode: 'fixture-text' (default, deterministic CI) or "
        "'pdf-text' (real text-based PDF extraction).",
    )
    parser.add_argument(
        "--pdf-path",
        default=None,
        help="Path to the PDF statement file. Required when --source-mode pdf-text.",
    )
    parser.add_argument(
        "--text-fixture",
        default=None,
        help=(
            "Path to the extracted-text fixture file (fixture-text mode only). "
            "Defaults to the checked-in fixture."
        ),
    )
    parser.add_argument(
        "--template",
        default=None,
        help="Template ID for row parsing. Defaults to 'sample_bank_v1'.",
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        help="Deterministic batch public_id (auto-generated when omitted).",
    )
    parser.add_argument(
        "--persist-review-queue",
        action="store_true",
        help=(
            "Persist review queue items to the reconciliation_review_queue table "
            "in the temp DB.  Cannot be used with :memory: databases.  "
            "Default: in-memory review queue only."
        ),
    )
    parser.add_argument(
        "--persistence-run-id",
        default=None,
        help="Stable run public_id for persistence.  Auto-generated when omitted.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the result as JSON instead of the default human-readable summary.",
    )
    return parser


__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
