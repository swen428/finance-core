"""Pure, bounded, deterministic total-level parser over normalized OCR blocks.

This module contains no database, network, clock, locale, or model access.
It transforms already-normalized receipt OCR blocks into a conservative,
review-oriented total-level personal-expense proposal payload.  Every
unresolved field stays explicitly null with a stable ambiguity flag so an
authenticated human can complete the workflow later.  The parser never
invents monetary facts and never applies a default currency.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Sequence

from finance_core.money import (
    SUPPORTED_CURRENCIES,
    MoneyValidationError,
    SignPolicy,
    canonical_money_str,
    money_decimal,
    normalize_currency,
    validate_amount_for_currency,
)

PARSER_CONTRACT_VERSION_DEFAULT = "receipt-total-proposal-v1"
PARSER_NAME = "receipt-total-ocr-parser"
PARSER_VERSION = "v1"

PROPOSAL_INTENT = "personal_expense_log"
PROPOSAL_TRANSACTION_TYPE = "personal_expense"

# Stable ambiguity / failure flags.
FLAG_TOTAL_NOT_FOUND = "total_not_found"
FLAG_CONFLICTING_TOTALS = "conflicting_total_candidates"
FLAG_TOTAL_AMOUNT_INVALID = "total_amount_invalid"
FLAG_AMBIGUOUS_CURRENCY_SYMBOL = "ambiguous_currency_symbol"
FLAG_CURRENCY_NOT_DETERMINED = "currency_not_determined"
FLAG_UNSUPPORTED_CURRENCY_FOR_AMOUNT = "unsupported_currency_for_amount"
FLAG_AMBIGUOUS_DATE = "ambiguous_transaction_date"
FLAG_CONFLICTING_DATES = "conflicting_date_candidates"
FLAG_DATE_NOT_FOUND = "transaction_date_not_found"
FLAG_MERCHANT_NOT_DETERMINED = "merchant_not_determined"
FLAG_OCR_NO_TEXT = "ocr_no_text"
FLAG_OCR_UNSUPPORTED_INPUT = "ocr_unsupported_input"
FLAG_OCR_ENGINE_FAILED = "ocr_engine_failed"
FLAG_OCR_RESOURCE_REJECTED = "ocr_resource_rejected"

RECEIPT_AMBIGUITY_FLAGS = frozenset(
    {
        FLAG_TOTAL_NOT_FOUND,
        FLAG_CONFLICTING_TOTALS,
        FLAG_TOTAL_AMOUNT_INVALID,
        FLAG_AMBIGUOUS_CURRENCY_SYMBOL,
        FLAG_CURRENCY_NOT_DETERMINED,
        FLAG_UNSUPPORTED_CURRENCY_FOR_AMOUNT,
        FLAG_AMBIGUOUS_DATE,
        FLAG_CONFLICTING_DATES,
        FLAG_DATE_NOT_FOUND,
        FLAG_MERCHANT_NOT_DETERMINED,
        FLAG_OCR_NO_TEXT,
        FLAG_OCR_UNSUPPORTED_INPUT,
        FLAG_OCR_ENGINE_FAILED,
        FLAG_OCR_RESOURCE_REJECTED,
    }
)

_OCR_FAILURE_FLAGS = {
    "no_text": FLAG_OCR_NO_TEXT,
    "unsupported_input": FLAG_OCR_UNSUPPORTED_INPUT,
    "engine_failed": FLAG_OCR_ENGINE_FAILED,
    "resource_rejected": FLAG_OCR_RESOURCE_REJECTED,
}

_MAX_EXCERPT = 120
_MAX_MERCHANT_LENGTH = 64
_LINE_BAND = 12

# Monetary number token: 12, 12.34, 1,234.56 (1-2 decimal places only).
_NUM_RE = re.compile(
    r"(?<![\dA-Za-z.,])(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?![\dA-Za-z.,])"
)

# Explicit total labels only; SUBTOTAL is excluded by the negative lookbehind.
_TOTAL_LABEL_RE = re.compile(r"(?<![A-Z])(GRAND\s+TOTAL|NET\s+TOTAL|AMOUNT\s+DUE|TOTAL)(?![A-Z])")

_NEGATIVE_KEYWORDS = (
    "SUBTOTAL",
    "SUB TOTAL",
    "TAX",
    "GST",
    "VAT",
    "SAVING",
    "TENDER",
    "BALANCE",
    "CASH",
    "CHANGE",
    "ROUND",
    "CARD",
    "VISA",
    "MASTER",
    "AMEX",
    "PAID",
    "DISCOUNT",
    "POINTS",
)

_MERCHANT_EXCLUDE_KEYWORDS = _NEGATIVE_KEYWORDS + (
    "TOTAL",
    "RECEIPT",
    "INVOICE",
    "UEN",
    "REG NO",
    "REG.",
    "TEL",
    "PHONE",
    "FAX",
    "EMAIL",
    "HTTP",
    "WWW",
)

_RECOGNIZED_CURRENCY_CODES = frozenset(SUPPORTED_CURRENCIES | {"MYR"})

_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_NUMERIC_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[/.](\d{1,2})[/.](\d{4})(?!\d)")
_ISO_YMD_SLASH_RE = re.compile(r"(?<!\d)(\d{4})/(\d{1,2})/(\d{1,2})(?!\d)")

_AMBIGUOUS_CURRENCY = "$"


@dataclass(frozen=True)
class ParserOcrBlock:
    """A minimal, immutable view of one persisted normalized OCR block."""

    sequence_index: int
    page_index: int
    text: str
    left: int
    top: int
    engine_line_index: int | None = None
    confidence_scaled: int | None = None
    engine_block_index: int | None = None
    engine_paragraph_index: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class OcrLayoutContext:
    """Internal context built after the complete normalized OCR hash verifies."""

    extraction_public_id: str
    normalized_result_hash: str
    blocks: tuple[ParserOcrBlock, ...]


def _proven_layout_lines(blocks: Sequence[ParserOcrBlock]) -> list[_Line]:
    """Keep legacy parser grouping only when complete source lineage proves it.

    Reused Tesseract line numbers and distinct Vision observations cannot be
    joined for role proof. Missing hierarchy or disjoint vertical spans deny
    the exemption; they do not change ordinary receipt parsing.
    """
    by_index = {block.sequence_index: block for block in blocks}
    proven: list[_Line] = []
    for line in _build_lines(blocks):
        members = [by_index[i] for i in line.block_sequence_indexes]
        identities = {
            (b.page_index, b.engine_block_index, b.engine_paragraph_index, b.engine_line_index)
            for b in members
        }
        first = members[0]
        if len(identities) != 1 or first.engine_block_index is None:
            continue
        if first.engine_line_index is not None and first.engine_paragraph_index is None:
            continue
        # A Vision observation has no engine line number; never assemble several
        # blocks into a fabricated line using only a shared geometric band.
        if first.engine_line_index is None and len(members) != 1:
            continue
        if len(members) > 1:
            if any(b.height is None or b.height <= 0 for b in members):
                continue
            if max(b.top for b in members) >= min(b.top + (b.height or 0) for b in members):
                continue
        proven.append(line)
    return proven


def proven_layout_line_indexes(blocks: Sequence[ParserOcrBlock]) -> tuple[frozenset[int], ...]:
    return tuple(frozenset(line.block_sequence_indexes) for line in _proven_layout_lines(blocks))


def explicit_item_line_groups(
    blocks: Sequence[ParserOcrBlock], *, currency: str
) -> tuple[frozenset[int], ...]:
    """Prove complete ITEM lines, preserving each line's full evidence group."""
    groups: list[frozenset[int]] = []
    for line in _proven_layout_lines(blocks):
        match = re.fullmatch(r"ITEM ([A-Z]{3}) ([0-9]+(?:\.[0-9]+)?)", line.text)
        if match is None or match[1] != currency:
            continue
        try:
            amount = validate_amount_for_currency(
                money_decimal(match[2], label="item role"), currency, label="item role"
            )
            SignPolicy.STRICTLY_POSITIVE.enforce(amount, label="item role")  # type: ignore[attr-defined]
        except MoneyValidationError:
            continue
        groups.append(frozenset(line.block_sequence_indexes))
    return tuple(groups)


@dataclass(frozen=True)
class ParsedField:
    """One resolved (or explicitly unresolved) proposal field with evidence."""

    value: object | None
    confidence: float | None
    block_sequence_indexes: tuple[int, ...]
    excerpt: str | None


@dataclass(frozen=True)
class ReceiptTotalParseResult:
    """Deterministic total-level parse over one OCR extraction."""

    merchant: ParsedField
    amount: ParsedField
    currency: ParsedField
    transaction_date: ParsedField
    description: ParsedField
    category: ParsedField
    overall_confidence: float | None
    ambiguity_flags: tuple[str, ...]


@dataclass(frozen=True)
class _Line:
    page_index: int
    text: str
    upper: str
    block_sequence_indexes: tuple[int, ...]
    order_key: tuple[int, int]


@dataclass
class _FlagSet:
    flags: list[str] = field(default_factory=list)

    def add(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)

    def sorted(self) -> tuple[str, ...]:
        return tuple(sorted(self.flags))


def _empty_field() -> ParsedField:
    return ParsedField(value=None, confidence=None, block_sequence_indexes=(), excerpt=None)


def parse_receipt_total(
    blocks: Sequence[ParserOcrBlock],
    *,
    extraction_status: str,
) -> ReceiptTotalParseResult:
    """Parse normalized OCR blocks into a conservative total-level proposal.

    ``extraction_status`` is the persisted receipt OCR extraction status.  Any
    non-``succeeded`` status yields an incomplete pending proposal with all
    financial fields null and a stable failure flag preserved.
    """
    flags = _FlagSet()

    if extraction_status != "succeeded":
        failure_flag = _OCR_FAILURE_FLAGS.get(extraction_status)
        if failure_flag is None:
            raise ValueError(f"Unknown OCR extraction status: {extraction_status!r}")
        flags.add(failure_flag)
        return _incomplete_result(flags)

    lines = _build_lines(blocks)
    confidence_by_seq = {
        block.sequence_index: block.confidence_scaled
        for block in blocks
        if block.confidence_scaled is not None
    }

    amount_field, currency_field = _parse_total(lines, confidence_by_seq, flags)
    date_field = _parse_date(lines, confidence_by_seq, flags)
    merchant_field = _parse_merchant(lines, confidence_by_seq, flags)

    overall = _overall_confidence((amount_field, currency_field, date_field, merchant_field))

    return ReceiptTotalParseResult(
        merchant=merchant_field,
        amount=amount_field,
        currency=currency_field,
        transaction_date=date_field,
        description=_empty_field(),
        category=_empty_field(),
        overall_confidence=overall,
        ambiguity_flags=flags.sorted(),
    )


def _incomplete_result(flags: _FlagSet) -> ReceiptTotalParseResult:
    return ReceiptTotalParseResult(
        merchant=_empty_field(),
        amount=_empty_field(),
        currency=_empty_field(),
        transaction_date=_empty_field(),
        description=_empty_field(),
        category=_empty_field(),
        overall_confidence=None,
        ambiguity_flags=flags.sorted(),
    )


def _build_lines(blocks: Sequence[ParserOcrBlock]) -> list[_Line]:
    groups: dict[tuple[object, ...], list[ParserOcrBlock]] = {}
    for block in sorted(blocks, key=lambda b: b.sequence_index):
        if block.engine_line_index is not None:
            key: tuple[object, ...] = (block.page_index, "line", block.engine_line_index)
        else:
            key = (block.page_index, "band", block.top // _LINE_BAND)
        groups.setdefault(key, []).append(block)

    lines: list[_Line] = []
    for members in groups.values():
        ordered = sorted(members, key=lambda b: (b.left, b.sequence_index))
        text = " ".join(b.text for b in ordered)
        page = ordered[0].page_index
        min_seq = min(b.sequence_index for b in members)
        lines.append(
            _Line(
                page_index=page,
                text=text,
                upper=text.upper(),
                block_sequence_indexes=tuple(b.sequence_index for b in ordered),
                order_key=(page, min_seq),
            )
        )
    lines.sort(key=lambda line: line.order_key)
    return lines


def _is_total_line(upper: str) -> bool:
    if _TOTAL_LABEL_RE.search(upper) is None:
        return False
    return not any(keyword in upper for keyword in _NEGATIVE_KEYWORDS)


def _parse_total(
    lines: Sequence[_Line],
    confidence_by_seq: dict[int, int],
    flags: _FlagSet,
) -> tuple[ParsedField, ParsedField]:
    # candidate -> (currency_marker, decimal_str) mapped to the supporting line.
    candidates: dict[tuple[str | None, str], _Line] = {}
    for line in lines:
        if not _is_total_line(line.upper):
            continue
        line_candidate = _extract_line_total(line.upper)
        if line_candidate is None:
            continue
        candidates.setdefault(line_candidate, line)

    if not candidates:
        flags.add(FLAG_TOTAL_NOT_FOUND)
        return _empty_field(), _empty_field()

    if len(candidates) > 1:
        flags.add(FLAG_CONFLICTING_TOTALS)
        return _empty_field(), _empty_field()

    (currency_marker, decimal_str), line = next(iter(candidates.items()))
    confidence = _line_confidence(line, confidence_by_seq)
    excerpt = _excerpt(line.text)
    seqs = line.block_sequence_indexes

    if currency_marker is None:
        flags.add(FLAG_CURRENCY_NOT_DETERMINED)
        return _empty_field(), _empty_field()
    if currency_marker == _AMBIGUOUS_CURRENCY:
        flags.add(FLAG_AMBIGUOUS_CURRENCY_SYMBOL)
        return _empty_field(), _empty_field()

    currency_field = ParsedField(
        value=currency_marker,
        confidence=confidence,
        block_sequence_indexes=seqs,
        excerpt=excerpt,
    )

    if currency_marker not in SUPPORTED_CURRENCIES:
        # Recognized alias (e.g. MYR) that the Money Contract cannot validate.
        flags.add(FLAG_UNSUPPORTED_CURRENCY_FOR_AMOUNT)
        return _empty_field(), currency_field

    try:
        normalized_currency = normalize_currency(currency_marker)
        amount = money_decimal(decimal_str, label="receipt total")
        amount = validate_amount_for_currency(amount, normalized_currency, label="receipt total")
        amount = SignPolicy.STRICTLY_POSITIVE.enforce(  # type: ignore[attr-defined]
            amount, label="receipt total"
        )
        canonical = canonical_money_str(amount, normalized_currency)
    except MoneyValidationError:
        flags.add(FLAG_TOTAL_AMOUNT_INVALID)
        return _empty_field(), currency_field

    amount_field = ParsedField(
        value=canonical,
        confidence=confidence,
        block_sequence_indexes=seqs,
        excerpt=excerpt,
    )
    return amount_field, currency_field


def _extract_line_total(upper_line: str) -> tuple[str | None, str] | None:
    """Return the single monetary total on a total line, or None if ambiguous."""
    matches = list(_NUM_RE.finditer(upper_line))
    monetary: list[tuple[str | None, str]] = []
    for match in matches:
        raw = match.group(1)
        currency = _detect_currency(upper_line, match)
        has_decimal = "." in raw
        if currency is None and not has_decimal:
            # A bare integer with no currency marker is not a defensible total.
            continue
        value = raw.replace(",", "")
        # Preserve a leading minus so the Money Contract rejects negatives
        # rather than silently emitting a positive expense amount.
        prefix = upper_line[: match.start()].rstrip()
        if prefix.endswith("-") or prefix.endswith("\u2212"):
            value = "-" + value
        monetary.append((currency, value))

    distinct = {item for item in monetary}
    if len(distinct) != 1:
        return None
    return next(iter(distinct))


def _detect_currency(upper_line: str, match: re.Match[str]) -> str | None:
    left = upper_line[: match.start()].rstrip()
    right = upper_line[match.end() :].lstrip()

    detected = _currency_from_left(left)
    if detected is not None:
        return detected
    return _currency_from_right(right)


def _currency_from_left(left: str) -> str | None:
    if not left:
        return None
    if left.endswith("US$"):
        return "USD"
    if left.endswith("S$"):
        return "SGD"
    tokens = left.split()
    if not tokens:
        return None
    last = tokens[-1]
    if last in _RECOGNIZED_CURRENCY_CODES:
        return last
    if last == "RM":
        return "MYR"
    if last == "$":
        return _AMBIGUOUS_CURRENCY
    return None


def _currency_from_right(right: str) -> str | None:
    if not right:
        return None
    if right.startswith("S$"):
        return "SGD"
    if right.startswith("US$"):
        return "USD"
    tokens = right.split()
    if not tokens:
        return None
    first = tokens[0].rstrip(".,;:")
    if first in _RECOGNIZED_CURRENCY_CODES:
        return first
    if first == "RM":
        return "MYR"
    if first == "$":
        return _AMBIGUOUS_CURRENCY
    return None


def _parse_date(
    lines: Sequence[_Line],
    confidence_by_seq: dict[int, int],
    flags: _FlagSet,
) -> ParsedField:
    resolved: dict[str, _Line] = {}
    saw_ambiguous = False

    for line in lines:
        for iso, canonical, ambiguous in _extract_dates(line.text):
            if ambiguous:
                saw_ambiguous = True
                continue
            if canonical is not None:
                resolved.setdefault(canonical, line)

    if len(resolved) == 1:
        canonical, line = next(iter(resolved.items()))
        return ParsedField(
            value=canonical,
            confidence=_line_confidence(line, confidence_by_seq),
            block_sequence_indexes=line.block_sequence_indexes,
            excerpt=_excerpt(line.text),
        )

    if len(resolved) > 1:
        flags.add(FLAG_CONFLICTING_DATES)
        return _empty_field()

    if saw_ambiguous:
        flags.add(FLAG_AMBIGUOUS_DATE)
    else:
        flags.add(FLAG_DATE_NOT_FOUND)
    return _empty_field()


def _extract_dates(text: str) -> list[tuple[str, str | None, bool]]:
    results: list[tuple[str, str | None, bool]] = []

    for match in _ISO_DATE_RE.finditer(text):
        year, month, day = (int(match.group(i)) for i in (1, 2, 3))
        canonical = _valid_calendar_date(year, month, day)
        results.append((match.group(0), canonical, canonical is None))

    for match in _ISO_YMD_SLASH_RE.finditer(text):
        year, month, day = (int(match.group(i)) for i in (1, 2, 3))
        canonical = _valid_calendar_date(year, month, day)
        results.append((match.group(0), canonical, canonical is None))

    for match in _NUMERIC_DATE_RE.finditer(text):
        first, second, year = (int(match.group(i)) for i in (1, 2, 3))
        canonical, ambiguous = _resolve_numeric_date(first, second, year)
        results.append((match.group(0), canonical, ambiguous))

    return results


def _resolve_numeric_date(first: int, second: int, year: int) -> tuple[str | None, bool]:
    first_can_be_day = 1 <= first <= 31
    second_can_be_day = 1 <= second <= 31
    first_can_be_month = 1 <= first <= 12
    second_can_be_month = 1 <= second <= 12

    day: int | None = None
    month: int | None = None
    if first > 12 and second_can_be_month:
        day, month = first, second
    elif second > 12 and first_can_be_month:
        month, day = first, second
    elif first_can_be_month and second_can_be_month:
        # Both orderings are plausible calendar months -> ambiguous.
        return None, True
    else:
        return None, True

    if day is None or month is None:
        return None, True
    if not (first_can_be_day or second_can_be_day):
        return None, True

    canonical = _valid_calendar_date(year, month, day)
    return canonical, canonical is None


def _valid_calendar_date(year: int, month: int, day: int) -> str | None:
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


def _parse_merchant(
    lines: Sequence[_Line],
    confidence_by_seq: dict[int, int],
    flags: _FlagSet,
) -> ParsedField:
    if not lines:
        flags.add(FLAG_MERCHANT_NOT_DETERMINED)
        return _empty_field()

    first_page = lines[0].page_index
    for line in lines:
        if line.page_index != first_page:
            break
        if _is_plausible_merchant(line):
            return ParsedField(
                value=line.text.strip(),
                confidence=_line_confidence(line, confidence_by_seq),
                block_sequence_indexes=line.block_sequence_indexes,
                excerpt=_excerpt(line.text),
            )

    flags.add(FLAG_MERCHANT_NOT_DETERMINED)
    return _empty_field()


def _is_plausible_merchant(line: _Line) -> bool:
    stripped = line.text.strip()
    if not (2 <= len(stripped) <= _MAX_MERCHANT_LENGTH):
        return False
    letters = sum(1 for char in stripped if char.isalpha())
    if letters < 2:
        return False
    if any(keyword in line.upper for keyword in _MERCHANT_EXCLUDE_KEYWORDS):
        return False
    if _ISO_DATE_RE.search(stripped) or _NUMERIC_DATE_RE.search(stripped):
        return False
    digits = sum(1 for char in stripped if char.isdigit())
    if digits > letters:
        return False
    return True


def _line_confidence(line: _Line, confidence_by_seq: dict[int, int]) -> float | None:
    values = [
        confidence_by_seq[seq] for seq in line.block_sequence_indexes if seq in confidence_by_seq
    ]
    if not values:
        return None
    mean = sum(values) / (len(values) * 10000)
    return round(mean, 4)


def _overall_confidence(fields: Sequence[ParsedField]) -> float | None:
    values = [f.confidence for f in fields if f.value is not None and f.confidence is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _excerpt(text: str) -> str:
    stripped = text.strip()
    if len(stripped) <= _MAX_EXCERPT:
        return stripped
    return stripped[:_MAX_EXCERPT]
