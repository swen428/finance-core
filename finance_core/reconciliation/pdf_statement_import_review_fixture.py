"""PDF Statement Import Review Fixture v1.

Deterministic, read-only fixture/report helper that consumes the PDF
Statement Bridge Batch Normalizer output and produces review-friendly
payloads for accepted and blocked parsed PDF statement rows.

Non-goals:

- No raw PDF parsing.
- No OCR.
- No database connection.
- No SQL execution.
- No file I/O (does not open ``attachment_path`` or any other file).
- No mutation of final financial records.
- No statement import persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import (
    PdfBlockedReason,
    PdfBlockedStatementRow,
    PdfStatementBatchNormalizationResult,
)
from finance_core.reconciliation.statement_import_contracts import StructuredStatementRow

# ---------------------------------------------------------------------------
# Review row dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfStatementImportReviewAcceptedRow:
    """A single accepted (valid, normalized) PDF statement row for review.

    Dashboard-safe fields only — no sensitive source evidence.
    """

    merchant_raw: str
    amount: Decimal
    currency: str
    transaction_date: date | None
    posted_date: date | None
    amount_direction: StatementAmountDirection
    statement_row_reference: str | None
    row_fingerprint: str


@dataclass(frozen=True)
class PdfStatementImportReviewBlockedRow:
    """A single blocked PDF statement row for review.

    Dashboard-safe fields only — no attachment_path or raw_row_text.
    """

    source_statement_id: str
    description: str
    blocked_reasons: tuple[PdfBlockedReason, ...]
    row_fingerprint: str
    source_page_number: int | None = None
    source_row_ref: str | None = None


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfStatementImportReviewSummary:
    """Batch-level summary for PDF statement import review."""

    total_rows: int
    accepted_count: int
    blocked_count: int
    has_blocked_rows: bool
    blocked_reason_counts: tuple[tuple[PdfBlockedReason, int], ...]


# ---------------------------------------------------------------------------
# Review fixture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfStatementImportReviewFixture:
    """A deterministic, read-only review fixture for PDF statement import.

    Separates accepted rows from blocked rows and provides a summary.
    All data is derived from a ``PdfStatementBatchNormalizationResult``.
    """

    accepted_rows: tuple[PdfStatementImportReviewAcceptedRow, ...]
    blocked_rows: tuple[PdfStatementImportReviewBlockedRow, ...]
    summary: PdfStatementImportReviewSummary

    @property
    def dashboard_safe(self) -> bool:
        """Always True — this fixture only exposes dashboard-safe fields."""
        return True


# ---------------------------------------------------------------------------
# Builders — accepted rows
# ---------------------------------------------------------------------------


def _build_review_accepted_row(
    row: StructuredStatementRow,
) -> PdfStatementImportReviewAcceptedRow:
    """Build a review-accepted row from a normalized structured statement row."""
    return PdfStatementImportReviewAcceptedRow(
        merchant_raw=row.merchant_raw,
        amount=row.amount,
        currency=row.currency,
        transaction_date=row.transaction_date,
        posted_date=row.posted_date,
        amount_direction=row.amount_direction or StatementAmountDirection.UNKNOWN,
        statement_row_reference=row.statement_row_reference,
        row_fingerprint=row.row_fingerprint or "",
    )


# ---------------------------------------------------------------------------
# Builders — blocked rows (dashboard-safe)
# ---------------------------------------------------------------------------


def _build_review_blocked_row(
    row: PdfBlockedStatementRow,
) -> PdfStatementImportReviewBlockedRow:
    """Build a review-blocked row without sensitive fields."""
    return PdfStatementImportReviewBlockedRow(
        source_statement_id=row.source_statement_id,
        description=row.description,
        blocked_reasons=row.blocked_reasons,
        row_fingerprint=row.row_fingerprint or "",
        source_page_number=row.source_page_number,
        source_row_ref=row.source_row_ref,
    )


# ---------------------------------------------------------------------------
# Builders — summary
# ---------------------------------------------------------------------------


def _build_review_summary(
    batch_result: PdfStatementBatchNormalizationResult,
) -> PdfStatementImportReviewSummary:
    """Build the batch-level review summary."""
    return PdfStatementImportReviewSummary(
        total_rows=batch_result.total_rows,
        accepted_count=batch_result.accepted_count,
        blocked_count=batch_result.blocked_count,
        has_blocked_rows=batch_result.has_blocked_rows,
        blocked_reason_counts=batch_result.blocked_reason_counts,
    )


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_pdf_statement_import_review_fixture(
    batch_result: PdfStatementBatchNormalizationResult,
) -> PdfStatementImportReviewFixture:
    """Build a review fixture from a batch normalization result.

    Accepted rows are projected to dashboard-safe fields only.
    Blocked rows also exclude ``attachment_path`` and ``raw_row_text``.
    Summary fields come directly from the batch result.
    Ordering preserves the input order from ``accepted_rows`` and
    ``blocked_rows``.
    """
    accepted = tuple(_build_review_accepted_row(row) for row in batch_result.accepted_rows)
    blocked = tuple(_build_review_blocked_row(row) for row in batch_result.blocked_rows)
    summary = _build_review_summary(batch_result)
    return PdfStatementImportReviewFixture(
        accepted_rows=accepted,
        blocked_rows=blocked,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Export helpers — dashboard-safe payload
# ---------------------------------------------------------------------------


def _accepted_row_to_dict(row: PdfStatementImportReviewAcceptedRow) -> dict[str, Any]:
    """Convert an accepted review row to a deterministic dict.

    None fields are excluded. Decimals are serialized as strings for
    JSON compatibility; dates use ISO format.
    """
    result: dict[str, Any] = {
        "merchant_raw": row.merchant_raw,
        "amount": str(row.amount),
        "currency": row.currency,
    }
    if row.transaction_date is not None:
        result["transaction_date"] = row.transaction_date.isoformat()
    if row.posted_date is not None:
        result["posted_date"] = row.posted_date.isoformat()
    result["amount_direction"] = row.amount_direction.value
    if row.statement_row_reference is not None:
        result["statement_row_reference"] = row.statement_row_reference
    result["row_fingerprint"] = row.row_fingerprint
    return result


def _blocked_row_to_dict(row: PdfStatementImportReviewBlockedRow) -> dict[str, Any]:
    """Convert a blocked review row to a deterministic dict.

    Excludes attachment_path and raw_row_text.
    """
    result: dict[str, Any] = {
        "source_statement_id": row.source_statement_id,
        "description": row.description,
        "blocked_reasons": [r.value for r in row.blocked_reasons],
        "row_fingerprint": row.row_fingerprint,
    }
    if row.source_page_number is not None:
        result["source_page_number"] = row.source_page_number
    if row.source_row_ref is not None:
        result["source_row_ref"] = row.source_row_ref
    return result


def _summary_to_dict(summary: PdfStatementImportReviewSummary) -> dict[str, Any]:
    """Convert a review summary to a deterministic dict."""
    return {
        "total_rows": summary.total_rows,
        "accepted_count": summary.accepted_count,
        "blocked_count": summary.blocked_count,
        "has_blocked_rows": summary.has_blocked_rows,
        "blocked_reason_counts": [
            {"reason": reason.value, "count": count}
            for reason, count in summary.blocked_reason_counts
        ],
    }


def export_pdf_statement_import_review_dashboard_payload(
    fixture: PdfStatementImportReviewFixture,
) -> dict[str, Any]:
    """Export a dashboard-safe review payload as a deterministic dict.

    Accepted rows: dashboard-safe fields only.
    Blocked rows: no ``attachment_path`` or ``raw_row_text``.
    Summary: batch-level counts and reason breakdown.

    The returned dict is suitable for JSON serialization and does not
    expose any sensitive source evidence.
    """
    return {
        "accepted": [_accepted_row_to_dict(r) for r in fixture.accepted_rows],
        "blocked": [_blocked_row_to_dict(r) for r in fixture.blocked_rows],
        "summary": _summary_to_dict(fixture.summary),
    }


# ---------------------------------------------------------------------------
# Export helpers — audit payload
# ---------------------------------------------------------------------------


def _blocked_row_to_audit_dict(row: PdfBlockedStatementRow) -> dict[str, Any]:
    """Convert a raw blocked row to an audit dict including source evidence."""
    result: dict[str, Any] = {
        "source_statement_id": row.source_statement_id,
        "source_content_hash": row.source_content_hash,
        "attachment_path": row.attachment_path,
        "description": row.description,
        "blocked_reasons": [r.value for r in row.blocked_reasons],
        "row_fingerprint": row.row_fingerprint,
        "raw_row_text": row.raw_row_text,
        "direction": row.direction,
        "direction_source": row.direction_source,
        "direction_confidence": row.direction_confidence,
        "review_status": row.review_status,
        "review_reason": row.review_reason,
        "original_amount_token": row.original_amount_token,
        "original_amount_sign": row.original_amount_sign,
        "parser_name": row.parser_name,
        "parser_version": row.parser_version,
        "template_name": row.template_name,
        "template_version": row.template_version,
        "evidence_contract_version": row.evidence_contract_version,
    }
    if row.source_page_number is not None:
        result["source_page_number"] = row.source_page_number
    if row.source_row_ref is not None:
        result["source_row_ref"] = row.source_row_ref
    if row.source_row_number is not None:
        result["source_row_number"] = row.source_row_number
    return result


def _accepted_row_to_audit_dict(row: StructuredStatementRow) -> dict[str, Any]:
    """Convert an accepted row to an audit dict including source evidence."""
    result: dict[str, Any] = {
        "merchant_raw": row.merchant_raw,
        "amount": str(row.amount),
        "currency": row.currency,
        "row_fingerprint": row.row_fingerprint,
    }
    if row.transaction_date is not None:
        result["transaction_date"] = row.transaction_date.isoformat()
    if row.posted_date is not None:
        result["posted_date"] = row.posted_date.isoformat()
    result["amount_direction"] = (row.amount_direction or StatementAmountDirection.UNKNOWN).value
    if row.statement_row_reference is not None:
        result["statement_row_reference"] = row.statement_row_reference
    if row.raw_row_payload is not None:
        result["raw_row_payload"] = row.raw_row_payload
    return result


def export_pdf_statement_import_review_audit_payload(
    batch_result: PdfStatementBatchNormalizationResult,
) -> dict[str, Any]:
    """Export a full audit payload with source evidence fields.

    This includes ``attachment_path`` and ``raw_row_text`` on blocked
    rows, and ``raw_row_payload`` on accepted rows for traceability.

    This function is deliberately separate from
    ``export_pdf_statement_import_review_dashboard_payload`` to prevent
    accidental leakage of sensitive data into dashboard exports.
    """
    accepted = [_accepted_row_to_audit_dict(r) for r in batch_result.accepted_rows]
    blocked = [_blocked_row_to_audit_dict(r) for r in batch_result.blocked_rows]
    summary = _summary_to_dict(_build_review_summary(batch_result))
    return {"accepted": accepted, "blocked": blocked, "summary": summary}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PdfStatementImportReviewAcceptedRow",
    "PdfStatementImportReviewBlockedRow",
    "PdfStatementImportReviewSummary",
    "PdfStatementImportReviewFixture",
    "build_pdf_statement_import_review_fixture",
    "export_pdf_statement_import_review_dashboard_payload",
    "export_pdf_statement_import_review_audit_payload",
]
