"""PDF Statement Parser Template CLI v1.

Lightweight template-driven CLI for parsing PDF bank/credit-card statements
into reviewable structured output.  The CLI accepts a PDF path and a
template ID, extracts raw text from the PDF, and applies template-driven
column mapping and normalization to produce structured review records.

Key safety properties:

- Defaults to **dry-run / review mode** -- no live database writes.
- Output is structured but explicitly **not a final financial fact**.
- No final transaction mutation, no settlement obligations, no live DB.
- Rows with unparseable fields are flagged with warnings, not silently
  accepted.
- Extraction limitations are honestly reported in output.

Usage::

    python -m finance_core.reconciliation.pdf_statement_template_cli \\
        --pdf path/to/statement.pdf \\
        --template sample_bank_v1

    python -m finance_core.reconciliation.pdf_statement_template_cli \\
        --pdf path/to/statement.pdf \\
        --template sample_bank_v1 \\
        --output-json out.json

    python -m finance_core.reconciliation.pdf_statement_template_cli \\
        --pdf path/to/statement.pdf \\
        --template sample_bank_v1 \\
        --json

    python -m finance_core.reconciliation.pdf_statement_template_cli --list-templates

Non-goals:
- No OCR (image-based PDFs will produce empty/no extraction).
- No database connection or SQL execution.
- No mutation of database/finance.db.
- No final financial record creation or settlement obligation generation.
- No Telegram/Metabase/OCR runtime changes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from finance_core.reconciliation.pdf_statement_evidence import (
    PDF_DIRECTION_ALIASES,
    PDF_EVIDENCE_CONTRACT_VERSION,
    PDF_TEMPLATE_PARSER_NAME,
    PDF_TEMPLATE_PARSER_VERSION,
    PDF_TEXT_EXTRACTION_VERSION,
    PdfAmountSignConvention,
    PdfDirectionConfidence,
    PdfDirectionSource,
    PdfOriginalAmountSign,
    PdfRowReviewStatus,
    collect_explicit_direction_evidence,
    original_amount_sign,
    parse_pdf_amount_token,
)
from finance_core.reconciliation.pdf_statement_extractor import (
    DEFAULT_PDF_RESOURCE_LIMITS,
    PdfExtractionResult,
    PdfResourceLimits,
    extract_text_from_pdf,
)
from finance_core.reconciliation.pdf_statement_template import (
    PdfStatementTemplate,
    get_template,
    list_templates,
)

# Backward-compatible private view retained for CLI v1 callers and tests.
_DIRECTION_MAP = {
    token: direction.value.upper() for token, direction in PDF_DIRECTION_ALIASES.items()
}
_DIRECTION_FIELD_SPLIT_RE = re.compile(r"[\s,;/|]+")

# ---------------------------------------------------------------------------
# Structured output row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedStatementRow:
    """A single normalized statement row produced by template-driven parsing."""

    source_path: str
    template_id: str
    source_account_label: str
    transaction_date: date | None
    posted_date: date | None
    description: str
    amount: Decimal | None
    currency: str
    amount_direction: str
    raw_line: str
    source_page_number: int | None = None
    row_index: int = 0
    parse_status: str = "ok"
    warnings: tuple[str, ...] = ()
    source_content_hash: str | None = None
    source_filename: str | None = None
    source_row_number: int | None = None
    source_row_ref: str | None = None
    source_text_excerpt: str = ""
    table_section_id: str | None = None
    parser_name: str = PDF_TEMPLATE_PARSER_NAME
    parser_version: str = PDF_TEMPLATE_PARSER_VERSION
    template_version: str = "pdf-template-v2"
    extraction_version: str = PDF_TEXT_EXTRACTION_VERSION
    evidence_contract_version: str = PDF_EVIDENCE_CONTRACT_VERSION
    direction_source: PdfDirectionSource = PdfDirectionSource.MISSING
    direction_confidence: PdfDirectionConfidence = PdfDirectionConfidence.NONE
    review_status: PdfRowReviewStatus = PdfRowReviewStatus.REVIEW_REQUIRED
    review_reason: str | None = None
    original_amount_token: str | None = None
    original_amount_sign: PdfOriginalAmountSign = PdfOriginalAmountSign.MISSING
    amount_sign_convention: PdfAmountSignConvention = PdfAmountSignConvention.UNSIGNED_EXPLICIT
    currency_token: str | None = None
    currency_source: str = "template_default"
    transaction_date_token: str | None = None
    posted_date_token: str | None = None


@dataclass(frozen=True)
class ParseResult:
    """Aggregate result from parsing a PDF statement with a template."""

    pdf_path: str
    template_id: str
    template: PdfStatementTemplate
    rows: tuple[ParsedStatementRow, ...]
    total_rows: int
    ok_count: int
    warning_count: int
    error_count: int
    extraction: PdfExtractionResult
    extraction_success: bool = True
    extraction_warnings: tuple[str, ...] = ()
    source_content_hash: str | None = None
    source_filename: str | None = None


# ---------------------------------------------------------------------------
# Direction map and helpers
# ---------------------------------------------------------------------------


def _parse_date(raw: str, fmt: str) -> date | None:
    """Parse a date using the one format explicitly declared by the template."""
    if not raw or not raw.strip():
        return None
    from datetime import datetime

    try:
        return datetime.strptime(raw.strip(), fmt).date()
    except ValueError:
        return None


def _parse_amount(raw: str) -> Decimal | None:
    """Parse a raw amount string into a Decimal.  Returns None on failure."""
    parsed = parse_pdf_amount_token(raw)
    return parsed.value if parsed is not None else None


@dataclass(frozen=True)
class _AmountCandidate:
    index: int
    token: str
    value: Decimal


_CONTEXT_NUMBER_LABELS = frozenset(
    {"ACCOUNT", "ACCT", "A/C", "REFERENCE", "REF", "ID", "NUMBER", "NO"}
)
_CURRENCY_TOKENS = frozenset({"RM", "MYR", "SGD", "S$", "USD", "$"})


def _amount_candidates(
    fields: list[str],
    *,
    date_indices: set[int],
    direction_indices: set[int],
) -> list[_AmountCandidate]:
    """Return plausible monetary tokens without treating years/IDs as amounts."""
    candidates: list[_AmountCandidate] = []
    for index, token in enumerate(fields):
        if index in date_indices or index in direction_indices:
            continue
        normalized = token.strip().strip(",")
        digits_only = normalized.lstrip("+-").replace(",", "")
        if digits_only.isdigit() and len(digits_only) == 4:
            year = int(digits_only)
            if 1900 <= year <= 2100:
                continue
        if digits_only.isdigit() and len(digits_only) >= 6:
            continue
        if index > 0 and fields[index - 1].strip(":").upper() in _CONTEXT_NUMBER_LABELS:
            continue
        value = _parse_amount(normalized)
        if value is None:
            continue
        candidate_token = normalized
        if index > 0 and fields[index - 1].upper() in _CURRENCY_TOKENS:
            candidate_token = f"{fields[index - 1]} {normalized}"
        candidates.append(_AmountCandidate(index=index, token=candidate_token, value=value))
    return candidates


def _infer_direction(
    raw_debit_credit: str | None,
    amount: Decimal | None,
    sign_rules: str,
) -> str:
    """Map an explicit direction token; amount sign never supplies direction."""
    if not raw_debit_credit:
        return "UNKNOWN"
    evidence = collect_explicit_direction_evidence((raw_debit_credit,))
    return evidence.direction.value.upper() if evidence.direction is not None else "UNKNOWN"


def _row_to_dict(row: ParsedStatementRow) -> dict[str, Any]:
    """Convert a ParsedStatementRow to a JSON-serializable dict."""
    return {
        "source_path": row.source_path,
        "template_id": row.template_id,
        "source_account_label": row.source_account_label,
        "transaction_date": row.transaction_date.isoformat() if row.transaction_date else None,
        "posted_date": row.posted_date.isoformat() if row.posted_date else None,
        "description": row.description,
        "amount": str(row.amount) if row.amount is not None else None,
        "currency": row.currency,
        "amount_direction": row.amount_direction,
        "raw_line": row.raw_line,
        "source_page_number": row.source_page_number,
        "row_index": row.row_index,
        "parse_status": row.parse_status,
        "warnings": list(row.warnings),
        "source_content_hash": row.source_content_hash,
        "source_filename": row.source_filename,
        "source_row_number": row.source_row_number,
        "source_row_ref": row.source_row_ref,
        "source_text_excerpt": row.source_text_excerpt,
        "parser_name": row.parser_name,
        "parser_version": row.parser_version,
        "template_version": row.template_version,
        "extraction_version": row.extraction_version,
        "evidence_contract_version": row.evidence_contract_version,
        "direction_source": row.direction_source.value,
        "direction_confidence": row.direction_confidence.value,
        "review_status": row.review_status.value,
        "review_reason": row.review_reason,
        "original_amount_token": row.original_amount_token,
        "original_amount_sign": row.original_amount_sign.value,
        "amount_sign_convention": row.amount_sign_convention.value,
        "currency_token": row.currency_token,
        "currency_source": row.currency_source,
        "transaction_date_token": row.transaction_date_token,
        "posted_date_token": row.posted_date_token,
    }


def _direction_field_contains_only_aliases(value: str) -> bool:
    tokens = [
        token.strip().strip(".:")
        for token in _DIRECTION_FIELD_SPLIT_RE.split(value.strip().upper())
        if token.strip().strip(".:")
    ]
    return bool(tokens) and all(token in PDF_DIRECTION_ALIASES for token in tokens)


def _parse_row_heuristic(
    raw_line: str,
    source_path: str,
    template: PdfStatementTemplate,
    row_index: int,
    *,
    source_content_hash: str | None = None,
    source_filename: str | None = None,
    source_page_number: int | None = None,
    source_row_number: int | None = None,
) -> ParsedStatementRow:
    """Parse a single text line using positional heuristics."""
    warnings: list[str] = []
    fields = (
        [part.strip() for part in raw_line.split(template.column_delimiter)]
        if template.column_delimiter
        else raw_line.split()
    )
    source_row_ref = (
        f"page-{source_page_number}:line-{source_row_number}"
        if source_page_number is not None and source_row_number is not None
        else None
    )

    if len(fields) < 2:
        return ParsedStatementRow(
            source_path=source_path,
            template_id=template.template_id,
            source_account_label=template.account_label,
            transaction_date=None,
            posted_date=None,
            description=raw_line,
            amount=None,
            currency=template.currency,
            amount_direction="UNKNOWN",
            raw_line=raw_line,
            source_page_number=source_page_number,
            row_index=row_index,
            parse_status="error",
            warnings=("Too few fields to parse",),
            source_content_hash=source_content_hash,
            source_filename=source_filename,
            source_row_number=source_row_number,
            source_row_ref=source_row_ref,
            source_text_excerpt=raw_line,
            parser_name=template.parser_name,
            parser_version=template.parser_version,
            template_version=template.template_version,
            extraction_version=template.extraction_version,
            evidence_contract_version=template.evidence_contract_version,
            review_status=PdfRowReviewStatus.UNSUPPORTED_LAYOUT,
            review_reason="unsupported_layout",
            amount_sign_convention=template.amount_sign_convention,
        )

    # Date extraction
    txn_date: date | None = None
    posted_date: date | None = None
    date_candidates: list[tuple[int, str, date]] = []
    for index, f in enumerate(fields):
        dt = _parse_date(f, template.date_format)
        if dt is not None:
            date_candidates.append((index, f, dt))

    if date_candidates:
        txn_date = date_candidates[0][2]
    if len(date_candidates) == 2:
        posted_date = date_candidates[1][2]
    elif len(date_candidates) > 2:
        warnings.append("Ambiguous date candidates")

    if txn_date is None:
        warnings.append("Could not parse transaction_date from line")

    date_indices = {candidate[0] for candidate in date_candidates}
    candidates = _amount_candidates(
        fields,
        date_indices=date_indices,
        direction_indices=set(),
    )
    amount: Decimal | None = None
    amount_raw: str | None = None
    amount_index: int | None = None
    if len(candidates) == 1:
        amount = candidates[0].value
        amount_raw = candidates[0].token
        amount_index = candidates[0].index
    elif len(candidates) > 1:
        warnings.append("Ambiguous amount candidates")
    else:
        warnings.append("Could not parse amount from line")

    # Direction is authoritative only in a formally marked field or the
    # versioned template's declared positional field. Description tokens are
    # never scanned for merchant semantics such as PAYMENT, FEE, or REFUND.
    debit_credit_raw: str | None = None
    direction_indices: set[int] = set()
    explicit_direction_fields: list[str] = []
    direction_source = PdfDirectionSource.MISSING
    direction_confidence = PdfDirectionConfidence.NONE
    invalid_direction = False
    ambiguous_direction = False
    unsupported_direction_layout = False
    formal_direction_present = False
    for index, f in enumerate(fields):
        upper = f.strip().upper().replace(",", "").replace(".", "")
        if upper.startswith("DIRECTION=") or upper.startswith("TYPE="):
            formal_direction_present = True
            direction_indices.add(index)
            marked_value = upper.partition("=")[2]
            evidence = collect_explicit_direction_evidence((marked_value,))
            if evidence.recognized_tokens and _direction_field_contains_only_aliases(marked_value):
                explicit_direction_fields.append(marked_value)
            else:
                invalid_direction = True

    positional_index = template.direction_field_index
    if template.direction_field_name is not None:
        positional_index = template.column_names_lower.index(
            template.direction_field_name.strip().lower()
        )
    if template.direction_field_offset_from_end is not None:
        positional_index = len(fields) - template.direction_field_offset_from_end
    if (
        positional_index is None
        and amount_index is not None
        and template.direction_field_offset_from_amount is not None
    ):
        positional_index = amount_index + template.direction_field_offset_from_amount
    if positional_index is not None:
        if positional_index < 0 or positional_index >= len(fields):
            warnings.append("Missing declared direction field")
        elif positional_index not in direction_indices:
            upper = fields[positional_index].strip().upper().replace(",", "").replace(".", "")
            direction_indices.add(positional_index)
            evidence = collect_explicit_direction_evidence((upper,))
            if evidence.recognized_tokens and _direction_field_contains_only_aliases(upper):
                explicit_direction_fields.append(upper)
            elif upper:
                unsupported_direction_layout = True
            else:
                warnings.append("Missing declared direction field")

    direction_evidence = collect_explicit_direction_evidence(explicit_direction_fields)
    if invalid_direction:
        direction_source = PdfDirectionSource.INVALID
    elif unsupported_direction_layout:
        direction_source = PdfDirectionSource.INVALID
        warnings.append("Unsupported direction field layout")
    elif direction_evidence.is_ambiguous:
        ambiguous_direction = True
        direction_source = PdfDirectionSource.INVALID
        warnings.append("Ambiguous direction indicators")
    elif direction_evidence.direction is not None:
        debit_credit_raw = direction_evidence.direction.value
        direction_source = (
            PdfDirectionSource.EXPLICIT_TOKEN
            if formal_direction_present
            else PdfDirectionSource.EXPLICIT_COLUMN
        )
        direction_confidence = PdfDirectionConfidence.HIGH

    # Description: everything not date/amount/direction
    skip_indices = date_indices | direction_indices
    if amount_index is not None:
        skip_indices.add(amount_index)
        if amount_index > 0 and fields[amount_index - 1].upper() in _CURRENCY_TOKENS:
            skip_indices.add(amount_index - 1)
    desc_parts = [f for index, f in enumerate(fields) if index not in skip_indices]
    description = " ".join(desc_parts).strip()

    # Explicit direction mapping only; sign is retained solely as evidence.
    direction = _infer_direction(debit_credit_raw, amount, template.amount_sign_rules)
    if invalid_direction:
        direction_source = PdfDirectionSource.INVALID
        direction_confidence = PdfDirectionConfidence.NONE
        warnings.append("Invalid explicit direction")
    elif ambiguous_direction:
        direction_source = PdfDirectionSource.INVALID
        direction_confidence = PdfDirectionConfidence.NONE
    elif unsupported_direction_layout:
        direction_source = PdfDirectionSource.INVALID
        direction_confidence = PdfDirectionConfidence.NONE
    elif debit_credit_raw is None:
        warnings.append("Missing explicit direction")

    amount_sign = original_amount_sign(amount_raw, amount)
    normalized_amount = abs(amount) if amount is not None else None

    review_status = PdfRowReviewStatus.AUTHORITATIVE
    review_reason: str | None = None
    parse_status = "ok"
    if invalid_direction or (amount is None and not candidates):
        review_status = PdfRowReviewStatus.REJECTED
        review_reason = "invalid_direction" if invalid_direction else "missing_amount"
        parse_status = "error"
    elif ambiguous_direction:
        review_status = PdfRowReviewStatus.REVIEW_REQUIRED
        review_reason = "ambiguous_direction"
        parse_status = "warning"
    elif unsupported_direction_layout:
        review_status = PdfRowReviewStatus.UNSUPPORTED_LAYOUT
        review_reason = "unsupported_layout"
        parse_status = "warning"
    elif warnings:
        review_status = PdfRowReviewStatus.REVIEW_REQUIRED
        review_reason = warnings[0].lower().replace(" ", "_")
        parse_status = "warning"

    return ParsedStatementRow(
        source_path=source_path,
        template_id=template.template_id,
        source_account_label=template.account_label,
        transaction_date=txn_date,
        posted_date=posted_date,
        description=description,
        amount=normalized_amount,
        currency=template.currency,
        amount_direction=direction,
        raw_line=raw_line,
        source_page_number=source_page_number,
        row_index=row_index,
        parse_status=parse_status,
        warnings=tuple(warnings),
        source_content_hash=source_content_hash,
        source_filename=source_filename,
        source_row_number=source_row_number,
        source_row_ref=source_row_ref,
        source_text_excerpt=raw_line,
        parser_name=template.parser_name,
        parser_version=template.parser_version,
        template_version=template.template_version,
        extraction_version=template.extraction_version,
        evidence_contract_version=template.evidence_contract_version,
        direction_source=direction_source,
        direction_confidence=direction_confidence,
        review_status=review_status,
        review_reason=review_reason,
        original_amount_token=amount_raw,
        original_amount_sign=amount_sign,
        amount_sign_convention=template.amount_sign_convention,
        currency_token=template.currency,
        currency_source="template_default",
        transaction_date_token=date_candidates[0][1] if date_candidates else None,
        posted_date_token=date_candidates[1][1] if len(date_candidates) == 2 else None,
    )


def parse_pdf_with_template(
    pdf_path: str | Path,
    template_id: str,
    *,
    limits: PdfResourceLimits = DEFAULT_PDF_RESOURCE_LIMITS,
) -> ParseResult:
    """Parse a PDF statement using a named template.

    This is the primary public entry point.  It extracts raw text from the
    PDF, applies the template's column mapping and normalization rules, and
    returns a ParseResult with structured rows and metadata.
    """
    path = Path(pdf_path).resolve()
    template = get_template(template_id)

    # Step 1: Extract text
    extraction = extract_text_from_pdf(str(path), limits=limits)
    # A template describes layout, not the engine that produced these bytes.
    template = replace(template, extraction_version=extraction.extraction_version)
    if not extraction.success:
        return ParseResult(
            pdf_path=str(path),
            template_id=template_id,
            template=template,
            rows=(),
            total_rows=0,
            ok_count=0,
            warning_count=0,
            error_count=0,
            extraction=extraction,
            extraction_success=False,
            extraction_warnings=extraction.warnings,
            source_content_hash=extraction.source_content_hash,
            source_filename=extraction.source_filename,
        )

    # Step 2: Extract text lines
    text_lines: list[tuple[int, int, str]] = []
    for page in extraction.pages:
        for line_obj in page.lines:
            text_lines.append((page.page_number, line_obj.line_number, line_obj.text))

    if not text_lines:
        return ParseResult(
            pdf_path=str(path),
            template_id=template_id,
            template=template,
            rows=(),
            total_rows=0,
            ok_count=0,
            warning_count=0,
            error_count=0,
            extraction=extraction,
            extraction_success=True,
            extraction_warnings=extraction.warnings,
            source_content_hash=extraction.source_content_hash,
            source_filename=extraction.source_filename,
        )

    # Step 3: Apply template line-skipping
    effective_lines = text_lines[template.skip_header_lines :]
    if template.skip_footer_lines > 0:
        effective_lines = effective_lines[: -template.skip_footer_lines]

    # Step 4: Parse each line
    parsed_rows: list[ParsedStatementRow] = []
    for idx, (page_number, line_number, raw_line) in enumerate(effective_lines):
        row = _parse_row_heuristic(
            raw_line=raw_line,
            source_path=str(path),
            template=template,
            row_index=idx,
            source_content_hash=extraction.source_content_hash,
            source_filename=extraction.source_filename,
            source_page_number=page_number,
            source_row_number=line_number,
        )
        parsed_rows.append(row)

    total_ok = sum(1 for r in parsed_rows if r.parse_status == "ok")
    total_warn = sum(1 for r in parsed_rows if r.parse_status == "warning")
    total_err = sum(1 for r in parsed_rows if r.parse_status == "error")

    return ParseResult(
        pdf_path=str(path),
        template_id=template_id,
        template=template,
        rows=tuple(parsed_rows),
        total_rows=len(parsed_rows),
        ok_count=total_ok,
        warning_count=total_warn,
        error_count=total_err,
        extraction=extraction,
        extraction_success=True,
        extraction_warnings=extraction.warnings,
        source_content_hash=extraction.source_content_hash,
        source_filename=extraction.source_filename,
    )


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_text_output(result: ParseResult) -> str:
    """Format a ParseResult as human-readable text."""
    parts: list[str] = []

    parts.append("=" * 65)
    parts.append("PDF Statement Parser Template CLI -- Review Output")
    parts.append("=" * 65)
    parts.append(f"  PDF:            {result.pdf_path}")
    parts.append(f"  Template:       {result.template_id}")
    parts.append(f"  Institution:    {result.template.institution_name}")
    parts.append(f"  Account:        {result.template.account_label}")
    parts.append(f"  Currency:       {result.template.currency}")
    parts.append("")

    if not result.extraction_success:
        parts.append(f"  Extraction:     FAILED -- {result.extraction.error_message}")
        parts.append("=" * 65)
        return "\n".join(parts)

    ex = result.extraction
    parts.append(f"  PDF pages:      {ex.total_pages}")
    parts.append(f"  Text lines:     {ex.total_lines}")
    for w in result.extraction_warnings:
        parts.append(f"  [!] {w}")
    parts.append("")

    parts.append(f"  Total rows:     {result.total_rows}")
    parts.append(f"  OK:             {result.ok_count}")
    parts.append(f"  Warnings:       {result.warning_count}")
    parts.append(f"  Errors:         {result.error_count}")
    parts.append("")

    if result.total_rows == 0:
        parts.append("No rows parsed.  Check that the template matches the PDF layout.")
        parts.append("=" * 65)
        return "\n".join(parts)

    parts.append(f"{'--- Parsed Rows ---':^65}")
    parts.append(
        f"{'#':>3}  {'Status':<7} {'Date':<12} {'Direction':<10} "
        f"{'Amount':>10} {'Currency':<8} {'Description'}"
    )
    parts.append("-" * 65)

    for row in result.rows:
        status_marker = {"ok": "OK", "warning": "WARN", "error": "ERROR"}.get(row.parse_status, "?")
        txn_str = row.transaction_date.isoformat() if row.transaction_date else "-"
        amt_str = f"{row.amount:,.2f}" if row.amount is not None else "-"
        desc = row.description if row.description else "(empty)"
        if len(desc) > 28:
            desc = desc[:25] + "..."

        parts.append(
            f"{row.row_index:>3}  {status_marker:<7} {txn_str:<12} "
            f"{row.amount_direction:<10} {amt_str:>10} "
            f"{row.currency:<8} {desc}"
        )

        for w in row.warnings:
            parts.append(f"       [!] {w}")

    parts.append("=" * 65)
    parts.append("End of review.")
    parts.append("")
    parts.append(
        "NOTE: This is structured parser output for review only. "
        "It is NOT a final financial record."
    )

    return "\n".join(parts)


def _format_json_output(result: ParseResult) -> str:
    """Format a ParseResult as JSON."""
    payload: dict[str, Any] = {
        "pdf_path": result.pdf_path,
        "review_only": True,
        "not_final_financial_record": True,
        "template_id": result.template_id,
        "institution_name": result.template.institution_name,
        "account_label": result.template.account_label,
        "currency": result.template.currency,
        "extraction_success": result.extraction_success,
        "extraction_warnings": list(result.extraction_warnings),
        "extraction": {
            "total_pages": result.extraction.total_pages,
            "total_lines": result.extraction.total_lines,
            "success": result.extraction.success,
            "error_message": result.extraction.error_message,
        },
        "summary": {
            "total_rows": result.total_rows,
            "ok_count": result.ok_count,
            "warning_count": result.warning_count,
            "error_count": result.error_count,
        },
        "rows": [_row_to_dict(r) for r in result.rows],
    }
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Argument parser and main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Template-driven PDF statement parser CLI (review mode).",
    )
    parser.add_argument(
        "--pdf",
        type=str,
        default=None,
        help="Path to the PDF statement file.",
    )
    parser.add_argument(
        "--template",
        type=str,
        default=None,
        help="Template ID to use for parsing (e.g. 'sample_bank_v1').",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="Write JSON output to a file instead of stdout.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output JSON to stdout instead of text format.",
    )
    parser.add_argument(
        "--list-templates",
        action="store_true",
        default=False,
        help="List all available template IDs and exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the PDF statement parser template CLI.

    Returns 0 on success, non-zero on error or invalid arguments.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    return _run_command(args)


def _run_command(args: argparse.Namespace) -> int:
    """Execute the parsed CLI command."""
    if args.list_templates:
        templates = list_templates()
        print("Available PDF Statement Templates:")
        for tid in templates:
            t = get_template(tid)
            print(f"  {tid:<30} {t.institution_name} ({t.statement_type}, {t.currency})")
        return 0

    if not args.pdf:
        print("Error: --pdf PATH is required for parsing.", file=sys.stderr)
        return 2

    if not args.template:
        print("Error: --template ID is required for parsing.", file=sys.stderr)
        return 2

    try:
        result = parse_pdf_with_template(args.pdf, args.template)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print("\nAvailable templates:", file=sys.stderr)
        for tid in list_templates():
            t = get_template(tid)
            print(f"  {tid} -- {t.institution_name}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.output_json:
        output = _format_json_output(result)
        out_path = Path(args.output_json)
        out_path.write_text(output, encoding="utf-8")
        print(f"JSON output written to: {out_path.resolve()}")
        _print_summary(result)
        return 0

    if args.json:
        print(_format_json_output(result))
        return 0

    print(_format_text_output(result))
    return 0


def _print_summary(result: ParseResult) -> None:
    """Print a one-line summary to stdout."""
    print(
        f"Summary: {result.total_rows} rows "
        f"({result.ok_count} ok, {result.warning_count} warn, "
        f"{result.error_count} err)"
    )


__all__ = [
    "ParsedStatementRow",
    "ParseResult",
    "parse_pdf_with_template",
    "main",
]
