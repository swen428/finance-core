"""PDF Statement Review CLI Fixture v1.

Deterministic, read-only CLI/demo fixture that displays accepted and
blocked parsed PDF statement rows using synthetic in-memory data.

Text output is the default; JSON output is available with --json.
Uses the existing PDF Statement Bridge Batch Normalizer and PDF
Statement Import Review Fixture for all normalization and review logic.

Usage:

    python -m finance_core.reconciliation.pdf_statement_review_cli
    python -m finance_core.reconciliation.pdf_statement_review_cli --json
    python -m finance_core.reconciliation.pdf_statement_review_cli --blocked-only
    python -m finance_core.reconciliation.pdf_statement_review_cli --accepted-only
    python -m finance_core.reconciliation.pdf_statement_review_cli --limit 5
    python -m finance_core.reconciliation.pdf_statement_review_cli --json --audit
    python -m finance_core.reconciliation.pdf_statement_review_cli --json --blocked-only

Non-goals:

- No raw PDF parsing.
- No OCR.
- No database connection or SQL execution.
- No file I/O (does not open attachment_path or any other file).
- No mutation of final financial records.
- No statement import persistence.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date
from decimal import Decimal

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import (
    ParsedPdfStatementRow,
    PdfStatementBatchNormalizationResult,
    normalize_pdf_statement_rows_batch,
)
from finance_core.reconciliation.pdf_statement_evidence import (
    PdfDirectionConfidence,
    PdfDirectionSource,
    PdfRowReviewStatus,
    original_amount_sign,
)
from finance_core.reconciliation.pdf_statement_import_review_fixture import (
    PdfStatementImportReviewFixture,
    build_pdf_statement_import_review_fixture,
    export_pdf_statement_import_review_audit_payload,
    export_pdf_statement_import_review_dashboard_payload,
)

# ---------------------------------------------------------------------------
# Synthetic rows -- deterministic, stable, in-memory only
# ---------------------------------------------------------------------------


def _build_synthetic_rows() -> list[ParsedPdfStatementRow]:
    """Build a stable set of synthetic PDF statement rows for demo/review.

    These rows span accepted and blocked scenarios so the CLI can
    exercise every review path without touching real PDFs or a database.
    """
    rows = [
        # -- Accepted rows --
        ParsedPdfStatementRow(
            source_statement_id="stmt-syn-001",
            attachment_path="/demo/stmt-2026-06.pdf",
            description="STARBUCKS COFFEE #8842",
            amount=Decimal("18.50"),
            currency="MYR",
            source_page_number=1,
            source_row_ref="p1r1",
            transaction_date=date(2026, 6, 1),
            posted_date=date(2026, 6, 2),
            raw_row_text="01/06 STARBUCKS COFFEE #8842  18.50",
            amount_direction=StatementAmountDirection.DEBIT,
        ),
        ParsedPdfStatementRow(
            source_statement_id="stmt-syn-001",
            attachment_path="/demo/stmt-2026-06.pdf",
            description="GRABFOOD *LUNCH DELIVERY",
            amount=Decimal("32.80"),
            currency="MYR",
            source_page_number=1,
            source_row_ref="p1r2",
            transaction_date=date(2026, 6, 3),
            posted_date=date(2026, 6, 4),
            raw_row_text="03/06 GRABFOOD *LUNCH DELIVERY  32.80",
            amount_direction=StatementAmountDirection.DEBIT,
        ),
        ParsedPdfStatementRow(
            source_statement_id="stmt-syn-001",
            attachment_path="/demo/stmt-2026-06.pdf",
            description="SALARY CREDIT JUNE",
            amount=Decimal("8000.00"),
            currency="MYR",
            source_page_number=2,
            source_row_ref="p2r1",
            transaction_date=date(2026, 6, 28),
            posted_date=date(2026, 6, 28),
            raw_row_text="28/06 SALARY CREDIT JUNE  8000.00 CR",
            amount_direction=StatementAmountDirection.CREDIT,
        ),
        ParsedPdfStatementRow(
            source_statement_id="stmt-syn-001",
            attachment_path="/demo/stmt-2026-06.pdf",
            description="TRANSFER TO SAVINGS",
            amount=Decimal("2000.00"),
            currency="MYR",
            source_page_number=2,
            source_row_ref="p2r2",
            transaction_date=date(2026, 6, 28),
            posted_date=date(2026, 6, 28),
            raw_row_text="28/06 TRANSFER TO SAVINGS  2000.00",
            amount_direction=StatementAmountDirection.PAYMENT,
        ),
        ParsedPdfStatementRow(
            source_statement_id="stmt-syn-001",
            attachment_path="/demo/stmt-2026-06.pdf",
            description="NETFLIX.COM SINGAPORE",
            amount=Decimal("49.90"),
            currency="SGD",
            source_page_number=3,
            source_row_ref="p3r1",
            transaction_date=date(2026, 6, 15),
            posted_date=date(2026, 6, 16),
            raw_row_text="15/06 NETFLIX.COM SINGAPORE  49.90 SGD",
            amount_direction=StatementAmountDirection.DEBIT,
        ),
        ParsedPdfStatementRow(
            source_statement_id="stmt-syn-001",
            attachment_path="/demo/stmt-2026-06.pdf",
            description="SHOPEE REFUND #ORD-9921",
            amount=Decimal("45.00"),
            currency="MYR",
            source_page_number=3,
            source_row_ref="p3r2",
            transaction_date=date(2026, 6, 10),
            posted_date=date(2026, 6, 12),
            raw_row_text="10/06 SHOPEE REFUND #ORD-9921  45.00 CR",
            amount_direction=StatementAmountDirection.REFUND,
        ),
        # -- Blocked rows --
        ParsedPdfStatementRow(
            source_statement_id="stmt-syn-001",
            attachment_path="/demo/stmt-2026-06.pdf",
            description="",
            amount=Decimal("5.00"),
            currency="MYR",
            source_page_number=1,
            source_row_ref="p1r3",
            transaction_date=date(2026, 6, 2),
            raw_row_text="02/06   5.00",
            amount_direction=StatementAmountDirection.DEBIT,
        ),
        ParsedPdfStatementRow(
            source_statement_id="stmt-syn-001",
            attachment_path="/demo/stmt-2026-06.pdf",
            description="MYSTERY CHARGE ???",
            amount=None,
            currency="MYR",
            source_page_number=1,
            source_row_ref="p1r4",
            transaction_date=date(2026, 6, 5),
            raw_row_text="05/06 MYSTERY CHARGE ???  --",
            amount_direction=StatementAmountDirection.DEBIT,
        ),
        ParsedPdfStatementRow(
            source_statement_id="stmt-syn-001",
            attachment_path="/demo/stmt-2026-06.pdf",
            description="LATE PAYMENT FEE",
            amount=Decimal("10.00"),
            currency="",
            source_page_number=2,
            source_row_ref="p2r3",
            transaction_date=date(2026, 6, 20),
            raw_row_text="20/06 LATE PAYMENT FEE  10.00",
            amount_direction=StatementAmountDirection.DEBIT,
        ),
        ParsedPdfStatementRow(
            source_statement_id="stmt-syn-001",
            attachment_path="/demo/stmt-2026-06.pdf",
            description="UNKNOWN TRANSACTION",
            amount=Decimal("99.00"),
            currency="MYR",
            source_page_number=3,
            source_row_ref="p3r3",
            transaction_date=None,
            posted_date=None,
            raw_row_text="--/-- UNKNOWN TRANSACTION  99.00",
            amount_direction=StatementAmountDirection.UNKNOWN,
        ),
    ]
    return [
        replace(
            row,
            source_content_hash="a" * 64,
            source_filename="synthetic-review.pdf",
            source_row_number=index,
            source_text_excerpt=row.raw_row_text,
            direction_source=PdfDirectionSource.EXPLICIT_TOKEN,
            direction_confidence=PdfDirectionConfidence.HIGH,
            review_status=PdfRowReviewStatus.AUTHORITATIVE,
            original_amount_token=str(row.amount) if row.amount is not None else None,
            original_amount_sign=original_amount_sign(
                str(row.amount) if row.amount is not None else None,
                row.amount,
            ),
            currency_token=row.currency,
            transaction_date_token=(
                row.transaction_date.isoformat() if row.transaction_date else None
            ),
            posted_date_token=row.posted_date.isoformat() if row.posted_date else None,
        )
        for index, row in enumerate(rows, start=1)
    ]


# ---------------------------------------------------------------------------
# Text output helpers
# ---------------------------------------------------------------------------


def _print_text_output(
    fixture: PdfStatementImportReviewFixture,
    *,
    accepted_only: bool = False,
    blocked_only: bool = False,
    limit: int | None = None,
) -> None:
    """Print human-readable text output for the review fixture."""
    summary = fixture.summary

    print("=" * 60)
    print("PDF Statement Review CLI -- Synthetic Demo")
    print("=" * 60)
    print()
    print(f"Total rows:      {summary.total_rows}")
    print(f"Accepted:        {summary.accepted_count}")
    print(f"Blocked:         {summary.blocked_count}")
    print(f"Has blocked:     {'Yes' if summary.has_blocked_rows else 'No'}")

    if summary.blocked_reason_counts:
        print()
        print("Blocked reason counts:")
        for reason, count in summary.blocked_reason_counts:
            print(f"  {reason.value:.<30} {count}")

    if not blocked_only:
        _print_accepted_rows(fixture, limit=limit)

    if not accepted_only:
        _print_blocked_rows(fixture, limit=limit)

    print()
    print("=" * 60)
    print("End of review.")


def _print_accepted_rows(
    fixture: PdfStatementImportReviewFixture,
    *,
    limit: int | None = None,
) -> None:
    """Print accepted row summaries."""
    rows = fixture.accepted_rows
    if limit is not None:
        rows = rows[:limit]

    if not rows:
        return

    print()
    print("--- Accepted Rows ---")
    for i, row in enumerate(rows, 1):
        txn = row.transaction_date.isoformat() if row.transaction_date else "no-date"
        posted = row.posted_date.isoformat() if row.posted_date else "no-date"
        print(
            f"  {i:>2}. {row.merchant_raw[:45]:<45} "
            f"{row.amount:>10} {row.currency:<4} "
            f"{row.amount_direction.value:<8} "
            f"txn={txn} posted={posted}"
        )


def _print_blocked_rows(
    fixture: PdfStatementImportReviewFixture,
    *,
    limit: int | None = None,
) -> None:
    """Print blocked row summaries with reason codes."""
    rows = fixture.blocked_rows
    if limit is not None:
        rows = rows[:limit]

    if not rows:
        return

    print()
    print("--- Blocked Rows ---")
    for i, row in enumerate(rows, 1):
        reasons = ", ".join(r.value for r in row.blocked_reasons)
        desc = row.description if row.description else "(empty)"
        print(f"  {i:>2}. {row.source_statement_id:<16} {desc[:40]:<40} reasons=[{reasons}]")


def _print_json_output(
    fixture: PdfStatementImportReviewFixture,
    batch_result: PdfStatementBatchNormalizationResult,
    *,
    audit: bool = False,
    accepted_only: bool = False,
    blocked_only: bool = False,
    limit: int | None = None,
) -> None:
    """Print JSON output using the existing review fixture export functions."""
    if audit:
        payload = export_pdf_statement_import_review_audit_payload(batch_result)
    else:
        payload = export_pdf_statement_import_review_dashboard_payload(fixture)

    if accepted_only:
        payload.pop("blocked", None)
    if blocked_only:
        payload.pop("accepted", None)
    if limit is not None:
        if "accepted" in payload:
            payload["accepted"] = payload["accepted"][:limit]
        if "blocked" in payload:
            payload["blocked"] = payload["blocked"][:limit]

    print(json.dumps(payload, indent=2, default=str))


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="pdf-statement-review",
        description="Read-only PDF statement review CLI fixture (synthetic data).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON instead of text.",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Include audit-level source evidence in JSON output. Requires --json.",
    )
    filter_group = parser.add_mutually_exclusive_group()
    filter_group.add_argument(
        "--blocked-only",
        action="store_true",
        help="Show only blocked rows.",
    )
    filter_group.add_argument(
        "--accepted-only",
        action="store_true",
        help="Show only accepted rows.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit output to the first N rows per section.",
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    """Validate CLI arguments and exit with a message for unsupported combos."""
    if args.audit and not args.json:
        print(
            "error: --audit requires --json. Audit payloads are JSON-only.",
            file=sys.stderr,
        )
        sys.exit(2)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Run the PDF statement review CLI fixture."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    _validate_args(args)

    rows = _build_synthetic_rows()
    batch_result = normalize_pdf_statement_rows_batch(rows)
    fixture = build_pdf_statement_import_review_fixture(batch_result)

    if args.json:
        _print_json_output(
            fixture,
            batch_result,
            audit=args.audit,
            accepted_only=args.accepted_only,
            blocked_only=args.blocked_only,
            limit=args.limit,
        )
    else:
        _print_text_output(
            fixture,
            accepted_only=args.accepted_only,
            blocked_only=args.blocked_only,
            limit=args.limit,
        )


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "main",
    "_build_synthetic_rows",
]
