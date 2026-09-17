"""Pure parsing helpers for statement CSV adapters."""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation

from finance_core.reconciliation.statement_csv_contracts import (
    AmountMode,
    CanonicalColumnMap,
    ColumnAliasMap,
    CsvDateParser,
    CsvRow,
)


def _resolve_columns(
    fieldnames: Sequence[str],
    alias_map: ColumnAliasMap,
    amount_mode: AmountMode,
) -> CanonicalColumnMap | None:
    """Map raw CSV header fieldnames to canonical column names."""
    header_lookup: dict[str, str] = {}
    for fn in fieldnames:
        key = _normalise_header(fn)
        header_lookup[key] = fn

    canonical: CanonicalColumnMap = {}

    for canonical_name, aliases in [
        ("posted_date", alias_map.posted_date_aliases),
        ("transaction_date", alias_map.txn_date_aliases),
        ("merchant_raw", alias_map.merchant_raw_aliases),
        ("currency", alias_map.currency_aliases),
        ("reference", alias_map.reference_aliases),
    ]:
        for alias in aliases:
            alias_key = _normalise_header(alias)
            if alias_key in header_lookup:
                canonical[canonical_name] = header_lookup.pop(alias_key)
                break
            for hkey, hval in list(header_lookup.items()):
                if alias_key in hkey.split():
                    canonical[canonical_name] = header_lookup.pop(hkey)
                    break
            if canonical_name in canonical:
                break

    for alias in alias_map.type_aliases:
        alias_key = _normalise_header(alias)
        if alias_key in header_lookup:
            canonical["type"] = header_lookup.pop(alias_key)
            break
        for hkey, hval in list(header_lookup.items()):
            if alias_key in hkey.split():
                canonical["type"] = header_lookup.pop(hkey)
                break
        if "type" in canonical:
            break

    if amount_mode == "signed":
        for alias in alias_map.amount_aliases:
            alias_key = _normalise_header(alias)
            if alias_key in header_lookup:
                canonical["amount"] = header_lookup[alias_key]
                break
    else:
        for alias in alias_map.debit_aliases:
            alias_key = _normalise_header(alias)
            if alias_key in header_lookup:
                canonical["debit"] = header_lookup[alias_key]
                break
        for alias in alias_map.credit_aliases:
            alias_key = _normalise_header(alias)
            if alias_key in header_lookup:
                canonical["credit"] = header_lookup[alias_key]
                break

    if "merchant_raw" not in canonical:
        return None
    if amount_mode == "signed" and "amount" not in canonical:
        return None
    if amount_mode == "debit_credit" and "debit" not in canonical and "credit" not in canonical:
        return None

    return canonical


def _normalise_header(raw: str) -> str:
    """Normalise a CSV header name for matching."""
    cleaned = re.sub(r"[/\\_-]", " ", raw)
    return re.sub(r"\s+", " ", cleaned.strip().lower())


def _get_cell(row_dict: Mapping[str, str], canonical: Mapping[str, str], key: str) -> str | None:
    """Safely extract a stripped cell value through a canonical mapping."""
    header = canonical.get(key)
    if header is None:
        return None
    raw = row_dict.get(header, "")
    if raw is None:
        return None
    val = raw.strip()
    return val if val else None


def _build_date_formats() -> list[tuple[str, CsvDateParser]]:
    """Build the ordered list of date format parsers."""
    return [
        ("YYYY-MM-DD", lambda s: date.fromisoformat(s)),
        ("DD/MM/YYYY", lambda s: datetime.datetime.strptime(s, "%d/%m/%Y").date()),
        ("DD-MM-YYYY", lambda s: datetime.datetime.strptime(s, "%d-%m-%Y").date()),
        ("DD MMM YYYY", lambda s: datetime.datetime.strptime(s, "%d %b %Y").date()),
        ("DD MMMM YYYY", lambda s: datetime.datetime.strptime(s, "%d %B %Y").date()),
    ]


_DATE_FORMATS: list[tuple[str, CsvDateParser]] = _build_date_formats()


def _parse_date(raw: str | None) -> date | None:
    """Parse an ISO date string, returning ``None`` for empty / invalid."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid date format: {raw!r}") from None


def _parse_date_flexible(raw: str) -> date:
    """Parse a date string using UK-style day-first formats."""
    raw = raw.strip()
    for _fmt_label, parser in _DATE_FORMATS:
        try:
            return parser(raw)
        except (ValueError, TypeError):
            continue
    raise ValueError(f"Unrecognised date format: {raw!r}")


_CURRENCY_SYMBOL_RE = re.compile(
    r"^(SGD|S\$|USD|\$|EUR|GBP|JPY|\xa5|AUD|MYR|RM|IDR|Rp|THB|\xe0\xb8\xbf|CNY|RMB)\s*",
    re.IGNORECASE,
)
_PAREN_NEGATIVE_RE = re.compile(r"^\((.+)\)$")


def _normalize_amount(raw: str) -> Decimal:
    """Normalise a raw amount string into a ``Decimal``."""
    raw = raw.strip()
    if not raw:
        raise ValueError("Empty amount string")

    paren_match = _PAREN_NEGATIVE_RE.match(raw)
    is_negative = False
    if paren_match:
        raw = paren_match.group(1).strip()
        is_negative = True

    raw = _CURRENCY_SYMBOL_RE.sub("", raw).strip()

    if raw.startswith("-"):
        is_negative = True
        raw = raw[1:].strip()
    elif raw.startswith("+"):
        raw = raw[1:].strip()

    raw = raw.replace(",", "")

    if not raw:
        raise ValueError("Empty amount after normalisation")

    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"Invalid amount: {raw!r}") from None

    if is_negative:
        value = -value

    return value


def _parse_signed_amount(raw: str) -> Decimal:
    """Parse a signed amount from a raw string."""
    return _normalize_amount(raw)


def _validate_currency(raw: str) -> str:
    """Validate and normalise a currency code."""
    cleaned = raw.strip().upper()
    if len(cleaned) != 3 or not cleaned.isalpha():
        raise ValueError(f"Invalid currency code: {cleaned!r}")
    return cleaned


def _csv_row_fingerprint(
    raw_row: CsvRow,
    canonical: Mapping[str, str] | None = None,
    *,
    source_content_hash: str | None = None,
    source_row_locator: str | None = None,
) -> str:
    """Generate a full SHA-256 fingerprint for CSV row identity material."""
    if canonical is not None:
        payload = {}
        for cname, header in sorted(canonical.items()):
            if header in raw_row:
                payload[cname] = raw_row[header]
    else:
        payload = dict(raw_row)

    material = {
        "row_contract_version": "statement-row-fingerprint-v1",
        "source_content_hash": source_content_hash,
        "source_row_locator": source_row_locator,
        "raw_row": payload,
    }
    canonical_json = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


__all__ = [
    "_csv_row_fingerprint",
    "_get_cell",
    "_normalize_amount",
    "_normalise_header",
    "_parse_date",
    "_parse_date_flexible",
    "_parse_signed_amount",
    "_resolve_columns",
    "_validate_currency",
]
