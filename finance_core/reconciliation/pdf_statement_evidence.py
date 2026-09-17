"""Versioned evidence and review-state contract for parsed PDF statement rows."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from finance_core.money import canonical_decimal_str, money_decimal
from finance_core.reconciliation.models import StatementAmountDirection

PDF_EVIDENCE_CONTRACT_VERSION = "pdf-row-evidence-v2"
PDF_ROW_FINGERPRINT_VERSION = "pdf-row-fingerprint-v2"
PDF_TEMPLATE_PARSER_NAME = "finance-pdf-template-parser"
PDF_TEMPLATE_PARSER_VERSION = "pdf-template-parser-v2"
PDF_TEXT_EXTRACTION_VERSION = "pypdf-text-extraction-v2"


class PdfRowReviewStatus(str, Enum):
    """Whether parser output is eligible for authoritative statement import."""

    AUTHORITATIVE = "authoritative"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"
    UNSUPPORTED_LAYOUT = "unsupported_layout"


class PdfDirectionSource(str, Enum):
    """Provenance for the direction classification."""

    EXPLICIT_TOKEN = "explicit_token"
    EXPLICIT_COLUMN = "explicit_column"
    TEMPLATE_ASSUMPTION = "template_assumption"
    MISSING = "missing"
    INVALID = "invalid"


class PdfDirectionConfidence(str, Enum):
    HIGH = "high"
    REVIEW = "review"
    NONE = "none"


class PdfOriginalAmountSign(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    ZERO = "zero"
    MISSING = "missing"
    INVALID = "invalid"


class PdfAmountSignConvention(str, Enum):
    """Versioned template rule for validating sign against explicit direction."""

    UNSIGNED_EXPLICIT = "unsigned_explicit"
    OUTFLOW_POSITIVE = "outflow_positive"
    OUTFLOW_NEGATIVE = "outflow_negative"


@dataclass(frozen=True)
class ParsedPdfAmountToken:
    """Deterministic interpretation of one original PDF amount token."""

    value: Decimal
    sign: PdfOriginalAmountSign
    currency_prefix: str | None = None


@dataclass(frozen=True)
class PdfCurrencyEvidenceResolution:
    """Deterministic comparison of source-token and authoritative currency evidence."""

    amount_token_prefix: str | None
    amount_token_currency: str | None
    currency_token: str | None
    currency_token_currency: str | None
    resolved_currency: str
    has_conflict: bool


@dataclass(frozen=True)
class PdfDirectionEvidence:
    """All recognized semantic directions in a parser evidence field set."""

    direction: StatementAmountDirection | None
    recognized_tokens: tuple[str, ...]
    distinct_directions: tuple[StatementAmountDirection, ...]

    @property
    def is_ambiguous(self) -> bool:
        return len(self.distinct_directions) > 1


_CURRENCY_PREFIXES = ("SGD", "MYR", "USD", "S$", "RM", "$")
_CURRENCY_PREFIX_CODES = {
    "S$": "SGD",
    "SGD": "SGD",
    "RM": "MYR",
    "MYR": "MYR",
    "USD": "USD",
}
_ISO_CURRENCY_RE = re.compile(r"[A-Z]{3}\Z")
_NUMBER_RE = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\Z")
_DIRECTION_SPLIT_RE = re.compile(r"[\s,;/|]+")
PDF_DIRECTION_ALIASES: dict[str, StatementAmountDirection] = {
    "D": StatementAmountDirection.DEBIT,
    "DR": StatementAmountDirection.DEBIT,
    "DEBIT": StatementAmountDirection.DEBIT,
    "PURCHASE": StatementAmountDirection.DEBIT,
    "C": StatementAmountDirection.CREDIT,
    "CR": StatementAmountDirection.CREDIT,
    "CREDIT": StatementAmountDirection.CREDIT,
    "DEPOSIT": StatementAmountDirection.CREDIT,
    "REFUND": StatementAmountDirection.REFUND,
    "REVERSAL": StatementAmountDirection.REVERSAL,
    "PAYMENT": StatementAmountDirection.PAYMENT,
    "FEE": StatementAmountDirection.FEE,
    "INTEREST": StatementAmountDirection.INTEREST,
    "CHARGEBACK": StatementAmountDirection.CHARGEBACK,
    "CARD_PAYMENT": StatementAmountDirection.CARD_PAYMENT,
    "TRANSFER_IN": StatementAmountDirection.TRANSFER_IN,
    "TRANSFER_OUT": StatementAmountDirection.TRANSFER_OUT,
    "INTEREST_DEBIT": StatementAmountDirection.INTEREST_DEBIT,
    "INTEREST_CREDIT": StatementAmountDirection.INTEREST_CREDIT,
    "CASH_WITHDRAWAL": StatementAmountDirection.CASH_WITHDRAWAL,
    "CASH_DEPOSIT": StatementAmountDirection.CASH_DEPOSIT,
}


_OUTGOING_DIRECTIONS = frozenset(
    {
        StatementAmountDirection.DEBIT,
        StatementAmountDirection.PAYMENT,
        StatementAmountDirection.CARD_PAYMENT,
        StatementAmountDirection.TRANSFER_OUT,
        StatementAmountDirection.FEE,
        StatementAmountDirection.INTEREST_DEBIT,
        StatementAmountDirection.CASH_WITHDRAWAL,
    }
)
_INCOMING_DIRECTIONS = frozenset(
    {
        StatementAmountDirection.CREDIT,
        StatementAmountDirection.REFUND,
        StatementAmountDirection.REVERSAL,
        StatementAmountDirection.CHARGEBACK,
        StatementAmountDirection.TRANSFER_IN,
        StatementAmountDirection.INTEREST_CREDIT,
        StatementAmountDirection.CASH_DEPOSIT,
    }
)


def parse_pdf_amount_token(token: str | None) -> ParsedPdfAmountToken | None:
    """Parse one complete amount token without discarding its source sign.

    Currency prefixes are accepted only at the beginning and longest prefixes
    are matched first. A prefix may be adjacent to the amount or followed by
    one ASCII space. Exactly one sign form may be used: leading plus/minus,
    enclosing parentheses, or trailing minus. Outer whitespace, whitespace
    after a leading sign, and whitespace just inside parentheses are rejected.
    """
    if token is None or not token or token != token.strip():
        return None
    remaining = token
    negative = False
    sign_count = 0

    if "(" in remaining or ")" in remaining:
        if not (remaining.startswith("(") and remaining.endswith(")")):
            return None
        remaining = remaining[1:-1]
        if not remaining or remaining != remaining.strip():
            return None
        negative = True
        sign_count += 1

    if remaining.endswith("-"):
        remaining = remaining[:-1]
        negative = True
        sign_count += 1

    leading_sign: str | None = None
    if remaining.startswith(("+", "-")):
        leading_sign = remaining[0]
        remaining = remaining[1:]
        sign_count += 1

    currency_prefix = next(
        (prefix for prefix in _CURRENCY_PREFIXES if remaining.upper().startswith(prefix)),
        None,
    )
    if currency_prefix is not None:
        remaining = remaining[len(currency_prefix) :]
        if remaining.startswith(" "):
            remaining = remaining[1:]

    if remaining.startswith(("+", "-")):
        if leading_sign is not None:
            return None
        leading_sign = remaining[0]
        remaining = remaining[1:]
        sign_count += 1

    if sign_count > 1 or not remaining or any(char.isspace() for char in remaining):
        return None
    if not _NUMBER_RE.fullmatch(remaining):
        return None
    if leading_sign == "-":
        negative = True

    try:
        magnitude = Decimal(remaining.replace(",", ""))
    except InvalidOperation:
        return None
    if not magnitude.is_finite():
        return None
    value = -magnitude if negative else magnitude
    sign = (
        PdfOriginalAmountSign.ZERO
        if magnitude == 0
        else PdfOriginalAmountSign.NEGATIVE
        if negative
        else PdfOriginalAmountSign.POSITIVE
    )
    return ParsedPdfAmountToken(
        value=value,
        sign=sign,
        currency_prefix=currency_prefix,
    )


def resolve_pdf_currency_evidence(
    *,
    original_amount_token: str | None,
    currency_token: str | None,
    row_currency: str,
) -> PdfCurrencyEvidenceResolution:
    """Resolve and compare every available PDF currency signal.

    ``$`` is deliberately context-dependent. It never supplies an independent
    currency code, while unambiguous amount prefixes and currency-column tokens
    must agree with each other and with the normalized authoritative row code.
    """
    parsed_amount = parse_pdf_amount_token(original_amount_token)
    amount_prefix = parsed_amount.currency_prefix if parsed_amount is not None else None
    amount_code = _CURRENCY_PREFIX_CODES.get(amount_prefix or "")

    raw_currency_token = currency_token.strip().upper() if isinstance(currency_token, str) else None
    if raw_currency_token == "":
        raw_currency_token = None
    if raw_currency_token == "$":
        token_code = None
        token_is_supported = True
    elif raw_currency_token in _CURRENCY_PREFIX_CODES:
        token_code = _CURRENCY_PREFIX_CODES[raw_currency_token]
        token_is_supported = True
    elif raw_currency_token is not None and _ISO_CURRENCY_RE.fullmatch(raw_currency_token):
        token_code = raw_currency_token
        token_is_supported = True
    else:
        token_code = None
        token_is_supported = raw_currency_token is None

    resolved = row_currency.strip().upper() if isinstance(row_currency, str) else ""
    explicit_codes = {code for code in (amount_code, token_code) if code is not None}
    has_conflict = (
        not token_is_supported
        or len(explicit_codes) > 1
        or bool(resolved and any(code != resolved for code in explicit_codes))
    )
    return PdfCurrencyEvidenceResolution(
        amount_token_prefix=amount_prefix,
        amount_token_currency=amount_code,
        currency_token=raw_currency_token,
        currency_token_currency=token_code,
        resolved_currency=resolved,
        has_conflict=has_conflict,
    )


def validate_canonical_normalized_amount_text(
    value: object,
    *,
    relational_amount: Decimal | None = None,
) -> Decimal:
    """Require exact, finite, non-exponent, scale-bounded canonical Decimal text."""
    try:
        if not isinstance(value, str) or value != value.strip():
            raise ValueError
        amount = money_decimal(value, label="PDF canonical normalized amount")
        if amount.is_zero() and amount.is_signed():
            raise ValueError
        normalized = amount.normalize()
        exponent = normalized.as_tuple().exponent
        if not isinstance(exponent, int) or exponent < -2:
            raise ValueError
        if canonical_decimal_str(amount) != value:
            raise ValueError
        if relational_amount is not None and amount != relational_amount:
            raise ValueError
        return amount
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError("invalid canonical normalized amount text") from exc


def original_amount_sign(
    token: str | None,
    value: Decimal | None = None,
) -> PdfOriginalAmountSign:
    """Derive sign from the complete source token; never trust ``value``."""
    del value
    if token is None or not token.strip():
        return PdfOriginalAmountSign.MISSING
    parsed = parse_pdf_amount_token(token)
    return parsed.sign if parsed is not None else PdfOriginalAmountSign.INVALID


def collect_explicit_direction_evidence(tokens: Iterable[str]) -> PdfDirectionEvidence:
    """Collect every recognized explicit direction before selecting one."""
    recognized: list[str] = []
    directions: set[StatementAmountDirection] = set()
    for raw in tokens:
        for token in _DIRECTION_SPLIT_RE.split(raw.strip().upper()):
            normalized = token.strip().strip(".:")
            direction = PDF_DIRECTION_ALIASES.get(normalized)
            if direction is None:
                continue
            recognized.append(normalized)
            directions.add(direction)
    distinct = tuple(sorted(directions, key=lambda item: item.value))
    return PdfDirectionEvidence(
        direction=distinct[0] if len(distinct) == 1 else None,
        recognized_tokens=tuple(recognized),
        distinct_directions=distinct,
    )


def sign_direction_is_compatible(
    *,
    sign: PdfOriginalAmountSign,
    direction: StatementAmountDirection,
    convention: PdfAmountSignConvention,
) -> bool:
    """Apply a deterministic, versioned sign/direction compatibility rule."""
    if sign in {PdfOriginalAmountSign.MISSING, PdfOriginalAmountSign.INVALID}:
        return False
    if sign is PdfOriginalAmountSign.ZERO:
        return False
    if direction in {StatementAmountDirection.UNKNOWN, StatementAmountDirection.INTEREST}:
        return False
    if convention is PdfAmountSignConvention.UNSIGNED_EXPLICIT:
        return not (sign is PdfOriginalAmountSign.NEGATIVE and direction in _OUTGOING_DIRECTIONS)
    if convention is PdfAmountSignConvention.OUTFLOW_POSITIVE:
        return (sign is PdfOriginalAmountSign.POSITIVE and direction in _OUTGOING_DIRECTIONS) or (
            sign is PdfOriginalAmountSign.NEGATIVE and direction in _INCOMING_DIRECTIONS
        )
    return (sign is PdfOriginalAmountSign.NEGATIVE and direction in _OUTGOING_DIRECTIONS) or (
        sign is PdfOriginalAmountSign.POSITIVE and direction in _INCOMING_DIRECTIONS
    )


__all__ = [
    "PDF_EVIDENCE_CONTRACT_VERSION",
    "PDF_DIRECTION_ALIASES",
    "PDF_ROW_FINGERPRINT_VERSION",
    "PDF_TEMPLATE_PARSER_NAME",
    "PDF_TEMPLATE_PARSER_VERSION",
    "PDF_TEXT_EXTRACTION_VERSION",
    "PdfAmountSignConvention",
    "PdfDirectionEvidence",
    "PdfCurrencyEvidenceResolution",
    "PdfDirectionConfidence",
    "PdfDirectionSource",
    "PdfOriginalAmountSign",
    "PdfRowReviewStatus",
    "ParsedPdfAmountToken",
    "collect_explicit_direction_evidence",
    "original_amount_sign",
    "parse_pdf_amount_token",
    "resolve_pdf_currency_evidence",
    "sign_direction_is_compatible",
    "validate_canonical_normalized_amount_text",
]
