"""Closed raw money-source qualification; normalization is risk detection only.

Every code point has one lexical owner. Unknown pieces invalidate the entire
assessment, so a later digit or a model quote cannot restart qualification.
Offsets are Python raw-string codepoint offsets, never normalized offsets.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from finance_core.money import SUPPORTED_CURRENCIES

SEPARATORS = " \t\r\n"
_WRAPPERS = ":;,!?，、；：！？。"
_UNKNOWN_CODES = frozenset({"CAD", "RMB", "MYR", "RM", "THB", "IDR", "KRW", "INR", "NZD", "CHF"})
_ROLE_WORDS = frozenset(
    {
        "TAX",
        "GST",
        "VAT",
        "NET",
        "TIP",
        "TEL",
        "FAX",
        "UEN",
        "QTY",
        "NOT",
        "AND",
        "THE",
        "NOW",
        "WAS",
        "OLD",
        "NEW",
        "BUT",
        "FOR",
    }
)
_CURRENCY_NAMES = ("元", "圆", "美元", "新币", "人民币", "日币", "日元", "港币", "欧元", "英镑")
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class Lexeme:
    start: int
    end: int
    kind: str
    raw: str
    currency: str | None = None


@dataclass(frozen=True)
class QualifiedMoneySpan:
    start: int
    end: int
    number_start: int
    number_end: int
    number: str
    currency: str | None


@dataclass(frozen=True)
class SourceMoneyAssessment:
    text: str
    partition: tuple[Lexeme, ...]
    spans: tuple[QualifiedMoneySpan, ...]
    currencies: frozenset[str]
    currency_state: str
    reasons: frozenset[str]
    dates: tuple[str, ...]

    @property
    def qualified(self) -> bool:
        return not self.reasons

    def supports_quote(self, quote: str, amount: str, *, source: str | None = None) -> bool:
        """An existing complete raw numeric span must lie within a quote.

        Qualification is global: a good occurrence cannot hide unknown or
        competing source notation. Quotes never get their own money tokenizer.
        """
        if not self.qualified or not _DECIMAL.fullmatch(amount):
            return False
        target = Decimal(amount)
        source = self.text if source is None else source
        bases = []
        start = 0
        while (offset := source.find(self.text, start)) >= 0:
            bases.append(offset)
            start = offset + 1
        offsets = []
        start = 0
        while (offset := source.find(quote, start)) >= 0:
            offsets.append((offset, offset + len(quote)))
            start = offset + 1
        return any(
            Decimal(span.number) == target
            and any(
                a <= base + span.number_start and base + span.number_end <= b
                for a, b in offsets
                for base in bases
            )
            for span in self.spans
        )


def _han(character: str) -> bool:
    return "\u3400" <= character <= "\u9fff" or "\U00020000" <= character <= "\U0003134f"


def _letter(character: str) -> bool:
    return unicodedata.category(character).startswith("L")


def _currency_word(raw: str) -> tuple[str, str | None]:
    shadow = unicodedata.normalize("NFKC", raw)
    upper = shadow.upper()
    literal_ascii = raw.isascii() and raw.isalpha()
    if literal_ascii and upper in SUPPORTED_CURRENCIES:
        return "currency", upper
    if upper in SUPPORTED_CURRENCIES or upper in _UNKNOWN_CODES:
        return "unknown_currency", None
    # Compatibility/mixed-script monetary words are never ordinary prose.
    if not literal_ascii and (
        re.fullmatch(r"[A-Z]{3}", upper)
        or len(raw) == 3
        and any(c.isascii() and c.isalpha() for c in raw)
        or any(code in upper for code in SUPPORTED_CURRENCIES | _UNKNOWN_CODES)
        or any(unicodedata.category(c) == "Sc" for c in shadow)
    ):
        return "unknown_currency", None
    if literal_ascii and re.fullmatch(r"[A-Z]{3}", raw) and upper not in _ROLE_WORDS:
        return "unknown_currency", None
    if any(name in raw for name in _CURRENCY_NAMES):
        return "unknown_currency", None
    return "prose", None


def assess_source_money(
    text: str, *, bare_currency: str | None = None, receipt_currency: str | None = None
) -> SourceMoneyAssessment:
    """Partition complete raw source; no Unicode rewriting grants support.

    Receipt aliases are available only after the existing genuine joint TOTAL
    owner has resolved that exact currency. No caller supplies model evidence.
    """
    tokens: list[Lexeme] = []
    reasons: set[str] = set()
    dates: list[str] = []
    index = 0
    while index < len(text):
        start, character = index, text[index]
        if character in SEPARATORS:
            index += 1
            while index < len(text) and text[index] in SEPARATORS:
                index += 1
            tokens.append(Lexeme(start, index, "separator", text[start:index]))
            continue
        match = _ISO_DATE.match(text, index)
        if match and (start == 0 or not (text[start - 1].isascii() and text[start - 1].isalnum())):
            end = match.end()
            if end == len(text) or not (text[end].isascii() and text[end].isalnum()):
                try:
                    date.fromisoformat(match[0])
                except ValueError:
                    pass
                else:
                    index = end
                    dates.append(match[0])
                    tokens.append(Lexeme(start, index, "date", match[0]))
                    continue
        if receipt_currency:
            alias = next(
                (
                    a
                    for a, c in (("US$", "USD"), ("S$", "SGD"))
                    if c == receipt_currency and text.startswith(a, index)
                ),
                None,
            )
            if alias:
                index += len(alias)
                tokens.append(Lexeme(start, index, "currency", alias, receipt_currency))
                continue
        if "0" <= character <= "9":
            index += 1
            while index < len(text) and ("0" <= text[index] <= "9" or text[index] == "."):
                index += 1
            raw = text[start:index]
            kind = "number" if _DECIMAL.fullmatch(raw) else "unknown"
            tokens.append(Lexeme(start, index, kind, raw))
            continue
        if _letter(character):
            is_han = _han(character)
            index += 1
            while index < len(text) and (
                _letter(text[index])
                and _han(text[index]) == is_han
                or unicodedata.category(text[index]).startswith("M")
            ):
                index += 1
            raw = text[start:index]
            kind, currency = _currency_word(raw)
            tokens.append(Lexeme(start, index, kind, raw, currency))
            continue
        if character == "$":
            kind = "dollar"
        elif character in _WRAPPERS:
            kind = "wrapper"
        elif character == ".":
            kind = "prose_period"
        elif (
            character in "-'_"
            and start
            and start + 1 < len(text)
            and _letter(text[start - 1])
            and _letter(text[start + 1])
        ):
            kind = "prose_connector"
        else:
            kind = "unknown_currency" if unicodedata.category(character) == "Sc" else "unknown"
        index += 1
        tokens.append(Lexeme(start, index, kind, character))

    significant = [token for token in tokens if token.kind != "separator"]
    spans: list[QualifiedMoneySpan] = []
    currencies: set[str] = set()
    has_currency_cue = False
    bound_codes: set[int] = set()
    for position, token in enumerate(significant):
        before = significant[position - 1] if position else None
        after = significant[position + 1] if position + 1 < len(significant) else None
        if token.kind in {"currency", "dollar", "unknown_currency"}:
            has_currency_cue = True
        if token.kind == "currency":
            assert token.currency is not None
            currencies.add(token.currency)
        if token.kind == "unknown_currency":
            reasons.add("unsupported_currency_notation")
        if token.kind == "unknown":
            reasons.add("unsupported_source_money_notation")
        if token.kind == "prose_period" and any(
            item
            and item.kind in {"number", "currency", "dollar"}
            and (item.end == token.start or token.end == item.start)
            for item in (before, after)
        ):
            reasons.add("unsupported_source_money_notation")
        if token.kind == "prose_connector" and any(
            item and item.kind != "prose" for item in (before, after)
        ):
            reasons.add("unsupported_source_money_notation")
        if token.kind == "prose" and after and after.kind in {"number", "currency", "dollar"}:
            if token.raw.casefold() in {"minus", "negative", "plus", "positive"} or (
                token.end == after.start and token.raw.endswith(("负", "负数", "正", "正数"))
            ):
                reasons.add("unsupported_source_money_notation")
        if (
            token.kind == "wrapper"
            and token.raw in {",", ";", ":"}
            and before
            and after
            and before.kind == after.kind == "number"
        ):
            reasons.add("unsupported_source_money_notation")
        if token.kind == "dollar":
            # Only the literal bare symbol may inherit exact known edit money.
            if (
                before
                and before.kind in {"prose", "currency", "unknown_currency"}
                and before.end == token.start
            ):
                reasons.add("unsupported_currency_notation")
            if not bare_currency or not after or after.kind != "number":
                reasons.add("ambiguous_currency_symbol")
            else:
                currencies.add(bare_currency)
        if token.kind != "number":
            continue
        nearby_codes = [
            (p, item)
            for p, item in ((position - 1, before), (position + 1, after))
            if item and item.kind in {"currency", "dollar"}
        ]
        if len(nearby_codes) > 1:
            reasons.add("unsupported_source_money_notation")
        currency = None
        first, last = token.start, token.end
        for code_position, code in nearby_codes:
            if code_position in bound_codes:
                reasons.add("unsupported_source_money_notation")
            bound_codes.add(code_position)
            currency = code.currency if code.kind == "currency" else bare_currency
            first, last = min(first, code.start), max(last, code.end)
        for item in (before, after):
            if not item:
                continue
            if item.kind == "number":
                # A separator does not turn malformed grouped money into two
                # independent candidates; separated true amounts still compete.
                pass
            elif (
                item.kind == "prose"
                and (item.end == token.start or item.start == token.end)
                and not all(_han(c) for c in item.raw)
            ):
                reasons.add("unsupported_source_money_notation")
            elif (
                item.kind == "prose"
                and re.fullmatch(r"[A-Za-z]{3}", item.raw)
                and item.raw.upper() not in _ROLE_WORDS
            ):
                reasons.add("unsupported_currency_notation")
        spans.append(QualifiedMoneySpan(first, last, token.start, token.end, token.raw, currency))
    if spans:
        for position, token in enumerate(significant):
            if token.kind == "currency" and position not in bound_codes:
                reasons.add("unsupported_currency_notation")
    if len(currencies) > 1:
        reasons.add("competing_currencies")
    if len(spans) > 1:
        reasons.add("competing_amounts")
    if reasons:
        state = "PRESENT_UNSUPPORTED_OR_AMBIGUOUS"
    elif has_currency_cue:
        state = "SUPPORTED_EXPLICIT" if currencies else "PRESENT_UNSUPPORTED_OR_AMBIGUOUS"
    else:
        state = "PROVEN_ABSENT"
    return SourceMoneyAssessment(
        text,
        tuple(tokens),
        tuple(spans),
        frozenset(currencies),
        state,
        frozenset(reasons),
        tuple(dates),
    )
