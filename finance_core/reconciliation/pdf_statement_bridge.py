"""PDF Statement Bridge Runtime Helper v1.

Deterministic normalization helper for already-parsed PDF statement rows.
Accepts ``ParsedPdfStatementRow`` objects and normalizes them into
``StructuredStatementRow``-compatible data. Does NOT parse raw PDF files.

Non-goals:

- No raw PDF parsing.
- No OCR.
- No pdfplumber, pytesseract, pypdf, camelot, tabula, or other PDF/OCR deps.
- No database connection.
- No SQL execution.
- No file I/O (does not open any file).
- No mutation of final financial records.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from finance_core.money import canonical_decimal_str
from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_evidence import (
    PDF_EVIDENCE_CONTRACT_VERSION,
    PDF_ROW_FINGERPRINT_VERSION,
    PDF_TEMPLATE_PARSER_NAME,
    PDF_TEMPLATE_PARSER_VERSION,
    PDF_TEXT_EXTRACTION_VERSION,
    PdfAmountSignConvention,
    PdfCurrencyEvidenceResolution,
    PdfDirectionConfidence,
    PdfDirectionSource,
    PdfOriginalAmountSign,
    PdfRowReviewStatus,
    parse_pdf_amount_token,
    resolve_pdf_currency_evidence,
    sign_direction_is_compatible,
    validate_canonical_normalized_amount_text,
)
from finance_core.reconciliation.statement_identity import canonical_row_fingerprint
from finance_core.reconciliation.statement_import_contracts import StructuredStatementRow

# ---------------------------------------------------------------------------
# Blocked reason enum
# ---------------------------------------------------------------------------


class PdfBlockedReason(Enum):
    """Stable reason codes for blocked PDF statement rows."""

    MISSING_AMOUNT = "missing_amount"
    AMBIGUOUS_DIRECTION = "ambiguous_direction"
    MISSING_CURRENCY = "missing_currency"
    CURRENCY_CONFLICT = "currency_conflict"
    MISSING_USABLE_DATE = "missing_usable_date"
    MISSING_DESCRIPTION = "missing_description"
    UNSUPPORTED_LAYOUT = "unsupported_layout"
    UNKNOWN_DIRECTION = "unknown_direction"
    INVALID_DIRECTION = "invalid_direction"
    NON_EXPLICIT_DIRECTION = "non_explicit_direction"
    SIGN_DIRECTION_CONTRADICTION = "amount_sign_contradiction"
    INVALID_AMOUNT = "invalid_amount"
    ZERO_AMOUNT_REQUIRES_REVIEW = "zero_amount_requires_review"
    MISSING_SOURCE_CONTENT_HASH = "missing_source_content_hash"
    MISSING_PAGE_EVIDENCE = "missing_page_evidence"
    MISSING_ROW_LOCATOR = "missing_row_locator"
    MISSING_SOURCE_EXCERPT = "missing_source_excerpt"
    MISSING_ATTACHMENT_EVIDENCE = "missing_attachment_evidence"
    MISSING_AMOUNT_TOKEN = "missing_amount_token"
    MISSING_DATE_TOKEN = "missing_date_token"
    MISSING_PARSER_VERSION = "missing_parser_version"
    MISSING_TEMPLATE_VERSION = "missing_template_version"
    INVALID_EVIDENCE_VERSION = "invalid_evidence_version"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Parsed PDF statement row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedPdfStatementRow:
    """A single parsed row from a PDF statement, before normalization.

    This is the canonical structure that a PDF parser must populate.
    It is read-only and carries no runtime logic.
    """

    source_statement_id: str
    attachment_path: str
    description: str
    amount: Decimal | None
    currency: str
    source_page_number: int | None = None
    source_row_ref: str | None = None
    transaction_date: date | None = None
    posted_date: date | None = None
    raw_row_text: str = ""
    external_reference: str | None = None
    amount_direction: StatementAmountDirection = StatementAmountDirection.UNKNOWN
    source_content_hash: str | None = None
    source_filename: str | None = None
    source_row_number: int | None = None
    source_text_excerpt: str = ""
    table_section_id: str | None = None
    parser_name: str = PDF_TEMPLATE_PARSER_NAME
    parser_version: str = PDF_TEMPLATE_PARSER_VERSION
    template_name: str = "legacy-pdf-template"
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


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def build_pdf_statement_row_fingerprint(row: ParsedPdfStatementRow) -> str:
    """Generate a deterministic SHA-256 fingerprint for a parsed PDF row.

    Format: full lowercase SHA-256; the version is persisted separately.

    Fingerprint inputs are intentionally limited to materially identifying
    fields. Volatile fields such as ``raw_row_text`` and timestamps are
    excluded so that re-parsing the same source row produces the same
    fingerprint.
    """
    currency_evidence = resolve_pdf_currency_evidence(
        original_amount_token=row.original_amount_token,
        currency_token=row.currency_token,
        row_currency=row.currency,
    )
    material = {
        "row_contract_version": PDF_ROW_FINGERPRINT_VERSION,
        "source_content_hash": row.source_content_hash,
        "page_number": row.source_page_number,
        "row_number": row.source_row_number,
        "row_locator": row.source_row_ref,
        "original_amount_token": row.original_amount_token,
        "original_amount_sign": _enum_value(row.original_amount_sign),
        "amount_sign_convention": _enum_value(row.amount_sign_convention),
        "normalized_amount": (
            canonical_decimal_str(row.amount)
            if isinstance(row.amount, Decimal) and row.amount.is_finite()
            else str(row.amount)
            if row.amount is not None
            else None
        ),
        "currency": _normalize_pdf_currency(row.currency),
        "currency_token": row.currency_token,
        "currency_source": row.currency_source,
        "currency_resolution": _currency_resolution_payload(currency_evidence),
        "direction": _enum_value(row.amount_direction),
        "direction_source": _enum_value(row.direction_source),
        "direction_confidence": _enum_value(row.direction_confidence),
        "transaction_date": row.transaction_date.isoformat() if row.transaction_date else None,
        "transaction_date_token": row.transaction_date_token,
        "posted_date": row.posted_date.isoformat() if row.posted_date else None,
        "posted_date_token": row.posted_date_token,
        "merchant_or_description": row.description,
        "table_section_id": row.table_section_id,
        "parser_name": row.parser_name,
        "parser_version": row.parser_version,
        "template_name": row.template_name,
        "template_version": row.template_version,
        "extraction_version": row.extraction_version,
        "evidence_contract_version": row.evidence_contract_version,
    }
    return canonical_row_fingerprint(material)


def _enum_value(value: object) -> str | None:
    raw = getattr(value, "value", value)
    return str(raw) if raw is not None else None


def _currency_resolution_payload(evidence: PdfCurrencyEvidenceResolution) -> dict[str, str]:
    payload = {"resolved_currency": evidence.resolved_currency}
    for field in (
        "amount_token_prefix",
        "amount_token_currency",
        "currency_token",
        "currency_token_currency",
    ):
        value = getattr(evidence, field)
        if value is not None:
            payload[field] = str(value)
    return payload


# ---------------------------------------------------------------------------
# Amount normalization
# ---------------------------------------------------------------------------


def _normalize_pdf_amount(value: Decimal) -> Decimal:
    """Normalize a PDF-row amount to non-negative Decimal.

    All ``StructuredStatementRow.amount`` values must be non-negative.
    The direction is carried separately via ``amount_direction``.
    """
    return value


_ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _normalize_pdf_currency(value: str) -> str:
    """Normalize a PDF-row currency code to uppercase ISO-3 shape."""
    return value.strip().upper() if value else ""


# ---------------------------------------------------------------------------
# Evidence payload
# ---------------------------------------------------------------------------


def build_pdf_statement_evidence_payload(
    row: ParsedPdfStatementRow,
    *,
    row_fingerprint: str,
) -> dict[str, Any]:
    """Build the ``raw_row_payload`` evidence dict for a PDF row."""
    currency_evidence = resolve_pdf_currency_evidence(
        original_amount_token=row.original_amount_token,
        currency_token=row.currency_token,
        row_currency=row.currency,
    )
    payload: dict[str, Any] = {
        "evidence_contract_version": row.evidence_contract_version,
        "row_fingerprint": row_fingerprint,
        "row_fingerprint_version": PDF_ROW_FINGERPRINT_VERSION,
        "source_content_hash": row.source_content_hash,
        "attachment_path": row.attachment_path,
        "source_filename": row.source_filename,
        "source_text_excerpt": row.source_text_excerpt,
        "original_line_text": row.raw_row_text,
        "raw_row_text": row.raw_row_text,
        "parser_name": row.parser_name,
        "parser_version": row.parser_version,
        "template_name": row.template_name,
        "template_version": row.template_version,
        "extraction_version": row.extraction_version,
        "direction": _enum_value(row.amount_direction),
        "direction_source": _enum_value(row.direction_source),
        "direction_confidence": _enum_value(row.direction_confidence),
        "review_status": _enum_value(row.review_status),
        "review_reason": row.review_reason,
        "original_amount_token": row.original_amount_token,
        "original_amount_sign": _enum_value(row.original_amount_sign),
        "amount_sign_convention": _enum_value(row.amount_sign_convention),
        "normalized_amount": (
            canonical_decimal_str(row.amount) if isinstance(row.amount, Decimal) else None
        ),
        "currency_token": row.currency_token,
        "currency_source": row.currency_source,
        "currency_resolution": _currency_resolution_payload(currency_evidence),
        "transaction_date_token": row.transaction_date_token,
        "posted_date_token": row.posted_date_token,
    }
    if row.source_page_number is not None:
        payload["source_page_number"] = row.source_page_number
    if row.source_row_number is not None:
        payload["source_row_number"] = row.source_row_number
    if row.source_row_ref is not None:
        payload["source_row_ref"] = row.source_row_ref
        payload["stable_row_locator"] = row.source_row_ref
    if row.table_section_id is not None:
        payload["table_section_id"] = row.table_section_id
    return payload


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfValidationResult:
    """Result of validating a parsed PDF statement row.

    Fields
    ------
    is_valid:
        True when the row passes all validation checks.
    is_blocked:
        True when the row must be blocked for human review.
    needs_review:
        True when the row requires human review (blocked or uncertain).
    blocked_reasons:
        Stable reason codes explaining why the row is blocked.
    """

    is_valid: bool
    is_blocked: bool = False
    needs_review: bool = False
    blocked_reasons: tuple[PdfBlockedReason, ...] = ()


def validate_pdf_statement_row(row: ParsedPdfStatementRow) -> PdfValidationResult:
    """Validate a parsed PDF statement row.

    Checks:
    - amount is present
    - amount direction is not UNKNOWN
    - currency is present and at least 3 characters
    - at least one usable date (transaction_date or posted_date)
    - description is present and non-empty
    - amount direction is not AMBIGUOUS (handled by UNKNOWN check)

    Returns a ``PdfValidationResult``.
    """
    reasons: list[PdfBlockedReason] = []

    amount_is_valid = True
    if row.amount is None:
        reasons.append(PdfBlockedReason.MISSING_AMOUNT)
        amount_is_valid = False
    elif not isinstance(row.amount, Decimal) or not row.amount.is_finite() or row.amount < 0:
        reasons.append(PdfBlockedReason.INVALID_AMOUNT)
        amount_is_valid = False
    elif row.amount == 0:
        reasons.append(PdfBlockedReason.ZERO_AMOUNT_REQUIRES_REVIEW)

    direction_is_supported = isinstance(row.amount_direction, StatementAmountDirection)
    if not direction_is_supported:
        reasons.append(PdfBlockedReason.INVALID_DIRECTION)
    elif row.amount_direction in {
        StatementAmountDirection.UNKNOWN,
        StatementAmountDirection.INTEREST,
    }:
        reasons.append(
            PdfBlockedReason.AMBIGUOUS_DIRECTION
            if row.review_reason == PdfBlockedReason.AMBIGUOUS_DIRECTION.value
            else PdfBlockedReason.UNKNOWN_DIRECTION
        )

    if row.direction_source is PdfDirectionSource.INVALID:
        reasons.append(PdfBlockedReason.INVALID_DIRECTION)
    elif row.direction_source not in {
        PdfDirectionSource.EXPLICIT_TOKEN,
        PdfDirectionSource.EXPLICIT_COLUMN,
    }:
        reasons.append(PdfBlockedReason.NON_EXPLICIT_DIRECTION)

    if row.direction_confidence is not PdfDirectionConfidence.HIGH:
        reasons.append(PdfBlockedReason.NON_EXPLICIT_DIRECTION)

    if not _is_full_sha256(row.source_content_hash or ""):
        reasons.append(PdfBlockedReason.MISSING_SOURCE_CONTENT_HASH)

    if (
        isinstance(row.source_page_number, bool)
        or not isinstance(row.source_page_number, int)
        or row.source_page_number < 1
    ):
        reasons.append(PdfBlockedReason.MISSING_PAGE_EVIDENCE)

    if row.source_row_number is not None and (
        isinstance(row.source_row_number, bool)
        or not isinstance(row.source_row_number, int)
        or row.source_row_number < 1
    ):
        reasons.append(PdfBlockedReason.MISSING_ROW_LOCATOR)

    if (
        not isinstance(row.source_row_ref, str)
        or not row.source_row_ref.strip()
        or row.source_row_ref != row.source_row_ref.strip()
    ):
        reasons.append(PdfBlockedReason.MISSING_ROW_LOCATOR)

    if (
        not isinstance(row.source_text_excerpt, str)
        or not row.source_text_excerpt.strip()
        or not isinstance(row.raw_row_text, str)
        or not row.raw_row_text.strip()
    ):
        reasons.append(PdfBlockedReason.MISSING_SOURCE_EXCERPT)

    if not isinstance(row.attachment_path, str) or not row.attachment_path.strip():
        reasons.append(PdfBlockedReason.MISSING_ATTACHMENT_EVIDENCE)

    if row.original_amount_token is None or not row.original_amount_token.strip():
        reasons.append(PdfBlockedReason.MISSING_AMOUNT_TOKEN)
        parsed_amount_token = None
    else:
        parsed_amount_token = parse_pdf_amount_token(row.original_amount_token)
        if parsed_amount_token is None:
            reasons.append(PdfBlockedReason.INVALID_AMOUNT)

    if parsed_amount_token is not None:
        if not isinstance(row.original_amount_sign, PdfOriginalAmountSign) or (
            row.original_amount_sign is not parsed_amount_token.sign
        ):
            reasons.append(PdfBlockedReason.SIGN_DIRECTION_CONTRADICTION)
        if (
            amount_is_valid
            and row.amount is not None
            and abs(parsed_amount_token.value) != row.amount
        ):
            reasons.append(PdfBlockedReason.SIGN_DIRECTION_CONTRADICTION)
        if (
            direction_is_supported
            and amount_is_valid
            and isinstance(row.amount_sign_convention, PdfAmountSignConvention)
            and not sign_direction_is_compatible(
                sign=parsed_amount_token.sign,
                direction=row.amount_direction,
                convention=row.amount_sign_convention,
            )
        ):
            reasons.append(PdfBlockedReason.SIGN_DIRECTION_CONTRADICTION)

    normalized_currency = _normalize_pdf_currency(row.currency)
    if not _ISO_CURRENCY_RE.fullmatch(normalized_currency):
        reasons.append(PdfBlockedReason.MISSING_CURRENCY)
    currency_evidence = resolve_pdf_currency_evidence(
        original_amount_token=row.original_amount_token,
        currency_token=row.currency_token,
        row_currency=normalized_currency,
    )
    if currency_evidence.has_conflict:
        reasons.append(PdfBlockedReason.CURRENCY_CONFLICT)

    if amount_is_valid and row.amount is not None:
        try:
            validate_canonical_normalized_amount_text(
                canonical_decimal_str(row.amount),
                relational_amount=row.amount,
            )
        except ValueError:
            reasons.append(PdfBlockedReason.INVALID_AMOUNT)

    if row.transaction_date is None and row.posted_date is None:
        reasons.append(PdfBlockedReason.MISSING_USABLE_DATE)
    if row.transaction_date is not None and not (row.transaction_date_token or "").strip():
        reasons.append(PdfBlockedReason.MISSING_DATE_TOKEN)
    if row.posted_date is not None and not (row.posted_date_token or "").strip():
        reasons.append(PdfBlockedReason.MISSING_DATE_TOKEN)

    if not row.description or not row.description.strip():
        reasons.append(PdfBlockedReason.MISSING_DESCRIPTION)

    if (
        not row.parser_name.strip()
        or not row.parser_version.strip()
        or not row.extraction_version.strip()
    ):
        reasons.append(PdfBlockedReason.MISSING_PARSER_VERSION)
    if not row.template_name.strip() or not row.template_version.strip():
        reasons.append(PdfBlockedReason.MISSING_TEMPLATE_VERSION)
    if row.evidence_contract_version != PDF_EVIDENCE_CONTRACT_VERSION:
        reasons.append(PdfBlockedReason.INVALID_EVIDENCE_VERSION)

    if row.review_status is PdfRowReviewStatus.REVIEW_REQUIRED:
        reasons.append(PdfBlockedReason.REVIEW_REQUIRED)
    elif row.review_status in {
        PdfRowReviewStatus.REJECTED,
        PdfRowReviewStatus.UNSUPPORTED_LAYOUT,
    }:
        reasons.append(PdfBlockedReason.REJECTED)
    elif row.review_status is not PdfRowReviewStatus.AUTHORITATIVE:
        reasons.append(PdfBlockedReason.REVIEW_REQUIRED)

    deduplicated_reasons = tuple(dict.fromkeys(reasons))
    is_blocked = len(deduplicated_reasons) > 0
    return PdfValidationResult(
        is_valid=not is_blocked,
        is_blocked=is_blocked,
        needs_review=is_blocked,
        blocked_reasons=deduplicated_reasons,
    )


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_pdf_statement_row(
    row: ParsedPdfStatementRow,
) -> StructuredStatementRow:
    """Normalize a parsed PDF row into a ``StructuredStatementRow``.

    Validation always runs before an authoritative structured row can exist.

    Returns a ``StructuredStatementRow`` with:
    - Non-negative amount.
    - Independent transaction_date / posted_date.
    - Amount direction classification.
    - Raw amount text preserved.
    - Source evidence in raw_row_payload.
    - Deterministic full SHA-256 row fingerprint.
    """
    result = validate_pdf_statement_row(row)
    if result.is_blocked:
        reasons = ", ".join(reason.value for reason in result.blocked_reasons)
        raise ValueError(f"PDF statement row is blocked: {reasons}")
    return _normalize_validated_pdf_statement_row(row)


def _normalize_validated_pdf_statement_row(
    row: ParsedPdfStatementRow,
) -> StructuredStatementRow:
    assert isinstance(row.amount, Decimal)

    normalized_amount = _normalize_pdf_amount(row.amount)
    fingerprint = build_pdf_statement_row_fingerprint(row)
    evidence = build_pdf_statement_evidence_payload(row, row_fingerprint=fingerprint)

    return StructuredStatementRow(
        merchant_raw=row.description,
        amount=normalized_amount,
        currency=_normalize_pdf_currency(row.currency),
        transaction_date=row.transaction_date,
        posted_date=row.posted_date,
        amount_direction=row.amount_direction,
        raw_amount=row.original_amount_token,
        raw_amount_type=(
            row.amount_direction.value
            if isinstance(row.amount_direction, StatementAmountDirection)
            else None
        ),
        statement_row_reference=row.source_row_ref,
        raw_row_payload=evidence,
        row_fingerprint=fingerprint,
        row_fingerprint_version=PDF_ROW_FINGERPRINT_VERSION,
        fingerprint_source_content_hash=row.source_content_hash,
    )


def _is_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def normalize_pdf_statement_row_checked(
    row: ParsedPdfStatementRow,
) -> StructuredStatementRow:
    """Validate and normalize a parsed PDF row into a ``StructuredStatementRow``.

    This is the safety-hardened variant of ``normalize_pdf_statement_row``.
    Validation runs first: if the row is blocked, a ``ValueError`` is raised
    with the blocked reasons. If valid, normalization proceeds as normal.

    Raises ``ValueError`` when the row fails validation.
    """
    return normalize_pdf_statement_row(row)


# ---------------------------------------------------------------------------
# Batch blocked row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfBlockedStatementRow:
    """A single PDF statement row that was blocked during batch normalization.

    Fields
    ------
    source_statement_id:
        Stable identity for the source statement.
    attachment_path:
        Path to the original PDF file preserved as source evidence.
    source_page_number:
        Page number within the PDF, if available.
    source_row_ref:
        Row/line reference within the page, if available.
    description:
        Raw merchant or transaction description text.
    blocked_reasons:
        Stable reason codes explaining why the row was blocked.
    raw_row_text:
        The raw text extracted from the PDF row.
    row_fingerprint:
        Deterministic fingerprint if safely buildable, or a blocked-row
        reference if the normal fingerprint cannot be built.
    """

    source_statement_id: str
    attachment_path: str
    source_page_number: int | None
    source_row_ref: str | None
    description: str
    blocked_reasons: tuple["PdfBlockedReason", ...]
    raw_row_text: str = ""
    row_fingerprint: str = ""
    source_content_hash: str | None = None
    source_row_number: int | None = None
    direction: str | None = None
    direction_source: str | None = None
    direction_confidence: str | None = None
    review_status: str | None = None
    review_reason: str | None = None
    original_amount_token: str | None = None
    original_amount_sign: str | None = None
    parser_name: str | None = None
    parser_version: str | None = None
    template_name: str | None = None
    template_version: str | None = None
    evidence_contract_version: str | None = None


def _build_blocked_row_fingerprint(row: ParsedPdfStatementRow) -> str:
    """Build the same versioned evidence fingerprint for a blocked row."""
    try:
        return build_pdf_statement_row_fingerprint(row)
    except Exception:
        return canonical_row_fingerprint(
            {
                "row_contract_version": PDF_ROW_FINGERPRINT_VERSION,
                "status": "blocked_unfingerprintable",
                "source_statement_id": row.source_statement_id,
                "source_content_hash": row.source_content_hash,
                "page_number": row.source_page_number,
                "row_locator": row.source_row_ref,
                "description": row.description,
            }
        )


# ---------------------------------------------------------------------------
# Batch normalization result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfStatementBatchNormalizationResult:
    """Result of normalizing multiple ``ParsedPdfStatementRow`` objects.

    Separates valid normalized rows from blocked rows without raising on
    the first blocked row.  Reason counts are deterministic and stable.

    Fields
    ------
    accepted_rows:
        Valid normalized ``StructuredStatementRow`` instances in input order.
    blocked_rows:
        Blocked ``PdfBlockedStatementRow`` instances in input order.
    total_rows:
        Total number of input rows processed.
    accepted_count:
        Number of rows accepted (len(accepted_rows)).
    blocked_count:
        Number of rows blocked (len(blocked_rows)).
    blocked_reason_counts:
        Sorted tuple of (reason, count) pairs for every blocked reason seen.
    has_blocked_rows:
        True when at least one row was blocked.
    """

    accepted_rows: tuple["StructuredStatementRow", ...]
    blocked_rows: tuple["PdfBlockedStatementRow", ...]
    total_rows: int
    accepted_count: int
    blocked_count: int
    blocked_reason_counts: tuple[tuple["PdfBlockedReason", int], ...]
    has_blocked_rows: bool


def _build_blocked_reason_counts(
    blocked_rows: list[PdfBlockedStatementRow],
) -> tuple[tuple[PdfBlockedReason, int], ...]:
    """Build a deterministic sorted tuple of (reason, count) pairs."""
    counts: dict[PdfBlockedReason, int] = {}
    for row in blocked_rows:
        for reason in row.blocked_reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return tuple(sorted(counts.items(), key=lambda item: item[0].value))


def _build_blocked_row(
    row: ParsedPdfStatementRow,
    result: PdfValidationResult,
) -> PdfBlockedStatementRow:
    """Build a ``PdfBlockedStatementRow`` from a parsed row and its
    validation result."""
    fingerprint = _build_blocked_row_fingerprint(row)
    return PdfBlockedStatementRow(
        source_statement_id=row.source_statement_id,
        attachment_path=row.attachment_path,
        source_page_number=row.source_page_number,
        source_row_ref=row.source_row_ref,
        description=row.description,
        blocked_reasons=result.blocked_reasons,
        raw_row_text=row.raw_row_text,
        row_fingerprint=fingerprint,
        source_content_hash=row.source_content_hash,
        source_row_number=row.source_row_number,
        direction=_enum_value(row.amount_direction),
        direction_source=_enum_value(row.direction_source),
        direction_confidence=_enum_value(row.direction_confidence),
        review_status=_enum_value(row.review_status),
        review_reason=row.review_reason,
        original_amount_token=row.original_amount_token,
        original_amount_sign=_enum_value(row.original_amount_sign),
        parser_name=row.parser_name,
        parser_version=row.parser_version,
        template_name=row.template_name,
        template_version=row.template_version,
        evidence_contract_version=row.evidence_contract_version,
    )


# ---------------------------------------------------------------------------
# Batch normalization function
# ---------------------------------------------------------------------------


def normalize_pdf_statement_rows_batch(
    rows: "Iterable[ParsedPdfStatementRow]",
) -> PdfStatementBatchNormalizationResult:
    """Normalize multiple parsed PDF rows, separating valid from blocked.

    Every row is validated with ``validate_pdf_statement_row()``.
    Valid rows are normalized with ``normalize_pdf_statement_row()``.
    Blocked rows are collected into ``PdfBlockedStatementRow`` instead
    of raising.  Input ordering is preserved independently for each
    output tuple.

    Returns a ``PdfStatementBatchNormalizationResult``.
    """
    accepted: list[StructuredStatementRow] = []
    blocked: list[PdfBlockedStatementRow] = []

    rows_list = list(rows)
    for row in rows_list:
        result = validate_pdf_statement_row(row)
        if result.is_blocked:
            blocked.append(_build_blocked_row(row, result))
        else:
            accepted.append(_normalize_validated_pdf_statement_row(row))

    reason_counts = _build_blocked_reason_counts(blocked)
    return PdfStatementBatchNormalizationResult(
        accepted_rows=tuple(accepted),
        blocked_rows=tuple(blocked),
        total_rows=len(rows_list),
        accepted_count=len(accepted),
        blocked_count=len(blocked),
        blocked_reason_counts=reason_counts,
        has_blocked_rows=len(blocked) > 0,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "PdfBlockedReason",
    "ParsedPdfStatementRow",
    "PdfValidationResult",
    "PdfBlockedStatementRow",
    "PdfStatementBatchNormalizationResult",
    "build_pdf_statement_row_fingerprint",
    "build_pdf_statement_evidence_payload",
    "validate_pdf_statement_row",
    "normalize_pdf_statement_row",
    "normalize_pdf_statement_row_checked",
    "normalize_pdf_statement_rows_batch",
]
