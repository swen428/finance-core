"""Statement CSV adapter contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, TypeAlias

from finance_core.reconciliation.statement_import_contracts import StructuredStatementRow

AmountMode: TypeAlias = Literal["signed", "debit_credit"]
CanonicalColumnMap: TypeAlias = dict[str, str]
CsvDateParser: TypeAlias = Callable[[str], date]
CsvRow: TypeAlias = dict[str, str]


@dataclass(frozen=True)
class RowValidationError:
    """A fine-grained per-row validation error produced during hardened parsing.

    Attributes:
        row_number: 1-based row number in the CSV file (header is line 1).
        raw_row: The raw CSV row dict for evidence.
        error_message: Human-readable error description.
        field: Optional canonical field name that caused the error.
    """

    row_number: int
    raw_row: CsvRow
    error_message: str
    field: str | None = None


@dataclass(frozen=True)
class ColumnAliasMap:
    """Configurable column alias mapping for statement CSV parsing.

    Each canonical field maps to a tuple of accepted column header names
    (case-insensitive, whitespace-normalised matching).
    """

    txn_date_aliases: tuple[str, ...] = (
        "transaction_date",
        "trans_date",
        "date",
        "txn date",
        "transaction date",
        "trans date",
    )
    posted_date_aliases: tuple[str, ...] = (
        "posted_date",
        "post_date",
        "posting date",
        "posted date",
        "value date",
    )
    merchant_raw_aliases: tuple[str, ...] = (
        "merchant",
        "description",
        "transaction description",
        "details",
        "narrative",
        "merchant_raw",
    )
    amount_aliases: tuple[str, ...] = (
        "amount",
        "transaction amount",
    )
    debit_aliases: tuple[str, ...] = (
        "debit",
        "debit amount",
        "paid out",
        "withdrawal",
    )
    credit_aliases: tuple[str, ...] = (
        "credit",
        "credit amount",
        "paid in",
        "deposit",
    )
    currency_aliases: tuple[str, ...] = (
        "currency",
        "ccy",
    )
    reference_aliases: tuple[str, ...] = (
        "reference",
        "ref",
        "transaction id",
        "statement row reference",
    )
    type_aliases: tuple[str, ...] = (
        "type",
        "transaction_type",
        "transaction type",
        "txn_type",
        "txn type",
    )


_DEFAULT_ALIAS_MAP = ColumnAliasMap()


@dataclass(frozen=True)
class CsvImportResult:
    """Result of parsing a CSV file into structured statement rows."""

    rows: list[StructuredStatementRow] = field(default_factory=list)
    error_count: int = 0
    skipped_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    validation_errors: list[RowValidationError] = field(default_factory=list)
    source_content_hash: str | None = None
    source_file_path: str | None = None
    source_filename: str | None = None

    @property
    def success(self) -> bool:
        return self.error_count == 0


__all__ = [
    "AmountMode",
    "CanonicalColumnMap",
    "ColumnAliasMap",
    "CsvDateParser",
    "CsvImportResult",
    "CsvRow",
    "RowValidationError",
]
