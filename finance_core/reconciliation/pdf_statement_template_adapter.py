"""PDF Statement Parser Template Adapter v1.

Converts ``ParsedStatementRow`` objects from the PDF template CLI parser
into ``ParsedPdfStatementRow`` objects suitable for the PDF statement
bridge helper and downstream reconciliation pipeline.

This module does NOT parse raw PDF files, connect to databases, or
mutate final financial records.

Non-goals:

- No raw PDF parsing.
- No OCR.
- No database connection or SQL execution.
- No file I/O (does not open ``attachment_path`` or any other file).
- No mutation of final financial records.
- No statement import persistence.
- No settlement obligation generation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import ParsedPdfStatementRow
from finance_core.reconciliation.pdf_statement_template_cli import (
    ParsedStatementRow,
    ParseResult,
)

# ---------------------------------------------------------------------------
# Direction mapping
# ---------------------------------------------------------------------------

_DIRECTION_TO_ENUM: dict[str, StatementAmountDirection] = {
    "DEBIT": StatementAmountDirection.DEBIT,
    "CREDIT": StatementAmountDirection.CREDIT,
    "REFUND": StatementAmountDirection.REFUND,
    "PAYMENT": StatementAmountDirection.PAYMENT,
    "FEE": StatementAmountDirection.FEE,
    "INTEREST": StatementAmountDirection.INTEREST,
    "REVERSAL": StatementAmountDirection.REVERSAL,
    "CHARGEBACK": StatementAmountDirection.CHARGEBACK,
    "CARD_PAYMENT": StatementAmountDirection.CARD_PAYMENT,
    "TRANSFER_IN": StatementAmountDirection.TRANSFER_IN,
    "TRANSFER_OUT": StatementAmountDirection.TRANSFER_OUT,
    "INTEREST_DEBIT": StatementAmountDirection.INTEREST_DEBIT,
    "INTEREST_CREDIT": StatementAmountDirection.INTEREST_CREDIT,
    "CASH_WITHDRAWAL": StatementAmountDirection.CASH_WITHDRAWAL,
    "CASH_DEPOSIT": StatementAmountDirection.CASH_DEPOSIT,
    "UNKNOWN": StatementAmountDirection.UNKNOWN,
}

# ---------------------------------------------------------------------------
# Adapter result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateAdapterResult:
    """Result of converting a ``ParseResult`` to ``ParsedPdfStatementRow`` objects.

    Carries the adapted rows ready for feed into the bridge batch
    normalizer, plus metadata about the source parse result. All rows
    are converted regardless of parse status -- error and warning rows
    flow through for bridge-level validation and blocking.

    Fields
    ------
    adapted_rows:
        Tuple of ``ParsedPdfStatementRow`` in original input order.
    total_rows:
        Total number of rows from the source ``ParseResult``.
    ok_count:
        Number of rows with ``parse_status == "ok"``.
    warning_count:
        Number of rows with ``parse_status == "warning"``.
    error_count:
        Number of rows with ``parse_status == "error"``.
    source_statement_id:
        Stable identifier derived from pdf_path and template_id.
    source_path:
        The original PDF path from the ``ParseResult`` (preserved as evidence).
    template_id:
        The template ID used for parsing.
    review_only:
        Always ``True`` -- output is for review, not final financial records.
    not_final_financial_record:
        Always ``True`` -- output is not a final financial record.
    """

    adapted_rows: tuple[ParsedPdfStatementRow, ...]
    total_rows: int
    ok_count: int
    warning_count: int
    error_count: int
    source_statement_id: str
    source_path: str
    template_id: str
    review_only: bool = True
    not_final_financial_record: bool = True


# ---------------------------------------------------------------------------
# Source statement ID
# ---------------------------------------------------------------------------


def _build_source_statement_id(
    source_content_hash: str | None,
    template_id: str,
    template_version: str = "pdf-template-v2",
) -> str:
    """Build a path-independent parser-run identity from content and template."""
    identity = source_content_hash if source_content_hash is not None else "unverified"
    raw = f"pdf-source-v2|{identity}|{template_id}|{template_version}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"pdf-tmpl-cli-{digest}"


# ---------------------------------------------------------------------------
# Direction conversion
# ---------------------------------------------------------------------------


def _convert_direction(amount_direction: str) -> StatementAmountDirection:
    """Map the CLI parser's string direction to ``StatementAmountDirection``."""
    return _DIRECTION_TO_ENUM.get(amount_direction, StatementAmountDirection.UNKNOWN)


# ---------------------------------------------------------------------------
# Single row conversion
# ---------------------------------------------------------------------------


def convert_parsed_statement_row_to_adapter_row(
    row: ParsedStatementRow,
    source_statement_id: str,
) -> ParsedPdfStatementRow:
    """Convert a single ``ParsedStatementRow`` into a ``ParsedPdfStatementRow``.

    Mapping rules (conservative):

    - ``source_path`` -> ``attachment_path`` (evidence preservation).
    - ``amount`` is passed through as-is (already absolute in the CLI parser;
      the bridge normalizer re-normalises as needed).
    - ``amount_direction`` is mapped from string to ``StatementAmountDirection``.
      Unrecognised values become ``UNKNOWN`` -- no guessing.
    - ``row_index`` -> ``source_row_ref`` as a stable string reference.
    - ``raw_line`` -> ``raw_row_text`` (evidence preservation).
    - ``source_page_number``, ``transaction_date``, ``posted_date`` are
      passed through directly.

    Parameters
    ----------
    row:
        A single parsed row from the template CLI parser.
    source_statement_id:
        Stable statement identifier (generated from pdf_path + template_id).

    Returns
    -------
    ParsedPdfStatementRow
        A bridge-compatible row for downstream normalization and review.
    """
    source_row_ref = row.source_row_ref or (
        f"page-{row.source_page_number}:line-{row.source_row_number}"
        if row.source_page_number is not None and row.source_row_number is not None
        else f"row-{row.row_index}"
    )

    return ParsedPdfStatementRow(
        source_statement_id=source_statement_id,
        attachment_path=row.source_path,
        description=row.description,
        amount=row.amount,  # already absolute
        currency=row.currency,
        source_page_number=row.source_page_number,
        source_row_number=(
            row.source_row_number if row.source_row_number is not None else row.row_index + 1
        ),
        source_row_ref=source_row_ref,
        transaction_date=row.transaction_date,
        posted_date=row.posted_date,
        raw_row_text=row.raw_line,
        amount_direction=_convert_direction(row.amount_direction),
        source_content_hash=row.source_content_hash,
        source_filename=row.source_filename,
        source_text_excerpt=row.source_text_excerpt or row.raw_line,
        table_section_id=row.table_section_id,
        parser_name=row.parser_name,
        parser_version=row.parser_version,
        template_name=row.template_id,
        template_version=row.template_version,
        extraction_version=row.extraction_version,
        evidence_contract_version=row.evidence_contract_version,
        direction_source=row.direction_source,
        direction_confidence=row.direction_confidence,
        review_status=row.review_status,
        review_reason=row.review_reason,
        original_amount_token=row.original_amount_token,
        original_amount_sign=row.original_amount_sign,
        amount_sign_convention=row.amount_sign_convention,
        currency_token=row.currency_token,
        currency_source=row.currency_source,
        transaction_date_token=row.transaction_date_token,
        posted_date_token=row.posted_date_token,
    )


# ---------------------------------------------------------------------------
# Aggregate conversion
# ---------------------------------------------------------------------------


def convert_template_parse_result_to_adapter_payload(
    parse_result: ParseResult,
) -> TemplateAdapterResult:
    """Convert an entire ``ParseResult`` into adapter-compatible structured rows.

    Every row in the ``ParseResult`` is converted, regardless of parse
    status. Error and warning rows flow through with their state reflected
    in the counts -- the bridge normalizer is responsible for blocking
    rows with missing critical fields.

    The ``review_only`` and ``not_final_financial_record`` flags are
    always ``True``. This adapter never creates final financial records.

    Parameters
    ----------
    parse_result:
        The aggregate parse result from ``parse_pdf_with_template()``.

    Returns
    -------
    TemplateAdapterResult
        Adapted rows with metadata, ready for ``normalize_pdf_statement_rows_batch()``.
    """
    source_statement_id = _build_source_statement_id(
        parse_result.source_content_hash,
        parse_result.template_id,
        parse_result.template.template_version,
    )

    adapted_rows: list[ParsedPdfStatementRow] = []
    ok_count = 0
    warning_count = 0
    error_count = 0

    for row in parse_result.rows:
        adapted_row = convert_parsed_statement_row_to_adapter_row(row, source_statement_id)
        adapted_rows.append(adapted_row)

        if row.parse_status == "ok":
            ok_count += 1
        elif row.parse_status == "warning":
            warning_count += 1
        else:
            error_count += 1

    return TemplateAdapterResult(
        adapted_rows=tuple(adapted_rows),
        total_rows=len(adapted_rows),
        ok_count=ok_count,
        warning_count=warning_count,
        error_count=error_count,
        source_statement_id=source_statement_id,
        source_path=parse_result.pdf_path,
        template_id=parse_result.template_id,
        review_only=True,
        not_final_financial_record=True,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "TemplateAdapterResult",
    "convert_parsed_statement_row_to_adapter_row",
    "convert_template_parse_result_to_adapter_payload",
]
