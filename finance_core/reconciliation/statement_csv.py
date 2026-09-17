"""Statement CSV Adapter v1.1 -- reads CSV bank/card statement rows and
converts them into ``StructuredStatementRow`` instances suitable for
the ``StatementImporter``.

---

# Column mapping hardening

The adapter now supports configurable column aliases via ``ColumnAliasMap``,
and the hardened parser auto-resolves common bank/credit-card column names
to canonical fields.

Canonical columns:

    transaction_date, posted_date, merchant_raw, amount, currency, reference

Supported column aliases:

    transaction_date: transaction_date, trans_date, date, txn date, transaction date
    posted_date:      posted_date, post_date, posting date, posted date, value date
    merchant_raw:     merchant, description, transaction description, details,
                      narrative, merchant_raw
    amount:           amount, transaction amount
    debit:            debit (for debit/credit split mode)
    credit:           credit (for debit/credit split mode)
    currency:         currency, ccy
    reference:        reference, ref, transaction id, statement row reference

---

# Amount normalization

Supports:

- single signed amount column (default)
- separate debit / credit columns
- debit as positive -> outgoing spend (positive amount for model)
- parentheses negatives, e.g. (12.34)
- comma thousands separators, e.g. 1,234.56
- currency symbols, e.g. SGD 12.34, S$12.34, $12.34
- blank debit or credit cells
- explicit negative signs

Sign policy:

- Outgoing spend -> positive amount (current model requires positive).
- Credits/refunds (credit-only rows, negative signed amounts, parens)
  are now classified by classify_row_direction and passed through with amount_direction set.
- Debit/credit split mode: debit->positive amount; credit-only rows rejected.

---

# Date parsing

Supports:

- YYYY-MM-DD (ISO)
- DD/MM/YYYY (UK day-first)
- DD-MM-YYYY (UK day-first)
- DD MMM YYYY (e.g. 01 Jan 2025)
- DD MMMM YYYY (e.g. 01 January 2025)

---

# Row fingerprint / idempotency

Each imported row gets a stable SHA-256 fingerprint over canonical
JSON of the raw row content.  Same row content -> same fingerprint;
different row content -> different fingerprint.

Format: full lowercase SHA-256; the version is persisted separately.

---

# Row-level validation

The hardened parser separates:

- successfully parsed rows
- validation errors (per-row, with row_number, raw_row, error_message, field)
- warnings

---

# Backward compatibility

The legacy ``_parse()`` method is preserved via ``_parse_legacy()``.
New entry points ``parse_file_hardened()`` and ``parse_text_hardened()``
use the hardened path.  ``CsvImportResult`` gains ``warnings`` and
``validation_errors`` fields (additive, backward compatible).

"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.statement_csv_contracts import (
    _DEFAULT_ALIAS_MAP,
    AmountMode,
    CanonicalColumnMap,
    ColumnAliasMap,
    CsvImportResult,
    CsvRow,
    RowValidationError,
)
from finance_core.reconciliation.statement_csv_parsing import (
    _csv_row_fingerprint,
    _get_cell,
    _normalize_amount,
    _parse_date,
    _parse_date_flexible,
    _parse_signed_amount,
    _resolve_columns,
    _validate_currency,
)
from finance_core.reconciliation.statement_identity import read_source_file_contents
from finance_core.reconciliation.statement_import_contracts import StructuredStatementRow

# ---------------------------------------------------------------------------
# Legacy required columns (for backward-compatible _parse_legacy)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Merchant direction keyword patterns for row classification
# ---------------------------------------------------------------------------
# Each tuple: (canonical direction, tuple of case-insensitive keyword patterns)
# Higher-priority patterns are checked first. These are conservative: a match
# requires the keyword to appear as a whole word or phrase in the merchant field.
_MERCHANT_DIRECTION_PATTERNS: tuple[tuple[StatementAmountDirection, tuple[str, ...]], ...] = (
    (
        StatementAmountDirection.PAYMENT,
        (
            "PAYMENT THANK YOU",
            "PAYMENT RECEIVED",
            "PAYMENT - THANK",
            "CARD PAYMENT",
            "INTERNET PAYMENT",
            "BILL PAYMENT",
            "PAYMENT TO",
            "PAYMENT FROM",
            "FUNDS TRANSFER",
        ),
    ),
    (
        StatementAmountDirection.REFUND,
        (
            "REFUND",
            "REVERSAL OF",
            "REVERSED",
            "REVERSAL",
            "CANCELLED",
            "CANCELED",
            "VOID",
        ),
    ),
    (
        StatementAmountDirection.FEE,
        (
            "ANNUAL FEE",
            "SERVICE FEE",
            "SERVICE CHARGE",
            "LATE FEE",
            "LATE PAYMENT CHARGE",
            "CARD FEE",
            "ADMIN FEE",
            "MONTHLY FEE",
            "ACCOUNT FEE",
            "BANK CHARGE",
        ),
    ),
    (
        StatementAmountDirection.INTEREST,
        (
            "INTEREST",
            "INTEREST CHARGE",
            "INTEREST CREDIT",
            "INTEREST PAID",
            "INTEREST EARNED",
        ),
    ),
)
_REQUIRED_COLUMNS: tuple[str, ...] = (
    "transaction_date",
    "merchant_raw",
    "amount",
    "currency",
)

_OPTIONAL_COLUMNS: tuple[str, ...] = (
    "posted_date",
    "merchant_normalized",
    "account_name",
    "account_id",
    "statement_row_reference",
    "raw_row_payload",
)


# ---------------------------------------------------------------------------
# Statement CSV Adapter
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Row direction classifier
# ---------------------------------------------------------------------------


def classify_row_direction(
    amount_raw: str | None,
    debit_raw: str | None,
    credit_raw: str | None,
    merchant_raw: str | None,
    amount_mode: AmountMode,
    *,
    raw_type: str | None = None,
    amount_parsed: Decimal | None = None,
) -> StatementAmountDirection:
    """Classify a statement row's amount direction based on deterministic rules.

    Classification order (first match wins):
    1. Explicit type column values (e.g. "REFUND", "PAYMENT", "FEE")
    2. Merchant name keyword patterns
    3. Amount sign / debit-credit column
    4. Default: DEBIT for positive amounts in signed mode,
       CREDIT for credit columns in debit-credit mode,
       UNKNOWN otherwise

    This function does not interpret or guess; ambiguous rows return UNKNOWN.
    """
    # --- 1. Explicit type column ---
    if raw_type:
        type_upper = raw_type.strip().upper()
        if type_upper in ("REFUND", "REVERSAL", "REVERSED", "CANCELLED"):
            return StatementAmountDirection.REFUND
        if type_upper in ("PAYMENT", "PAYMENT RECEIVED", "PAYMENT THANK YOU"):
            return StatementAmountDirection.PAYMENT
        if type_upper in (
            "FEE",
            "SERVICE FEE",
            "LATE FEE",
            "ANNUAL FEE",
            "BANK FEE",
            "ADMIN FEE",
            "SERVICE CHARGE",
            "BANK CHARGE",
        ):
            return StatementAmountDirection.FEE
        if "INTEREST" in type_upper:
            return StatementAmountDirection.INTEREST
        if type_upper in ("DEBIT", "PURCHASE", "SALE"):
            return StatementAmountDirection.DEBIT
        if type_upper in ("CREDIT"):
            return StatementAmountDirection.CREDIT

    # --- 2. Merchant name keyword patterns ---
    if merchant_raw:
        m_upper = merchant_raw.strip().upper()
        for direction, keywords in _MERCHANT_DIRECTION_PATTERNS:
            for kw in keywords:
                if kw in m_upper:
                    return direction

    # --- 3. Amount sign / column mode ---
    if amount_mode == "signed" and amount_parsed is not None:
        if amount_parsed > 0:
            return StatementAmountDirection.DEBIT
        if amount_parsed < 0:
            return StatementAmountDirection.UNKNOWN  # could be credit/refund; let review decide

    if amount_mode == "debit_credit":
        if debit_raw and not credit_raw:
            return StatementAmountDirection.DEBIT
        if credit_raw and not debit_raw:
            return StatementAmountDirection.CREDIT
        # Both present -> ambiguous
        if debit_raw and credit_raw:
            return StatementAmountDirection.UNKNOWN

    # --- 4. Default ---
    return StatementAmountDirection.UNKNOWN


def _classify_and_enrich(
    amount_raw: str | None,
    debit_raw: str | None,
    credit_raw: str | None,
    merchant_raw: str | None,
    amount_mode: AmountMode,
    *,
    raw_type: str | None = None,
    amount_parsed: Decimal | None = None,
) -> tuple[StatementAmountDirection, str | None]:
    """Classify direction and return the raw amount string for preservation.

    Returns (direction, raw_amount_string). The raw_amount_string preserves
    the original amount value from the CSV for audit purposes.
    """
    direction = classify_row_direction(
        amount_raw=amount_raw,
        debit_raw=debit_raw,
        credit_raw=credit_raw,
        merchant_raw=merchant_raw,
        amount_mode=amount_mode,
        raw_type=raw_type,
        amount_parsed=amount_parsed,
    )
    return direction, amount_raw


class StatementCsvAdapter:
    """Reads CSV statement files and produces ``StructuredStatementRow``
    instances.

    Usage (legacy path)::

        adapter = StatementCsvAdapter()
        result = adapter.parse_file("statement.csv")

    Usage (hardened path)::

        adapter = StatementCsvAdapter()
        result = adapter.parse_file_hardened("statement.csv")
    """

    _required = _REQUIRED_COLUMNS

    # ------------------------------------------------------------------
    # Public API: legacy path
    # ------------------------------------------------------------------

    def parse_file(self, path: str | Path) -> CsvImportResult:
        """Parse a CSV file at ``path`` into structured rows.

        Uses the legacy parser for backward compatibility.
        For hardened parsing with column aliases, amount normalisation,
        and date flexibility, use ``parse_file_hardened()``.
        """
        path = Path(path)
        source = read_source_file_contents(path)
        return self._parse_legacy(
            io.StringIO(source.content.decode("utf-8")),
            source=str(path),
            source_content_hash=source.evidence.content_hash,
            source_file_path=str(path),
            source_filename=source.evidence.original_filename,
        )

    def parse_text(self, text: str, *, source: str = "<string>") -> CsvImportResult:
        """Parse CSV text into structured rows.

        Uses the legacy parser for backward compatibility.
        For hardened parsing, use ``parse_text_hardened()``.
        """
        with io.StringIO(text) as fh:
            return self._parse_legacy(fh, source=source)

    # ------------------------------------------------------------------
    # Public API: hardened path
    # ------------------------------------------------------------------

    def parse_file_hardened(
        self,
        path: str | Path,
        *,
        alias_map: ColumnAliasMap | None = None,
        amount_mode: AmountMode = "signed",
    ) -> CsvImportResult:
        """Parse a CSV file with the hardened parser.

        Parameters:
            path: Path to the CSV file.
            alias_map: Optional ``ColumnAliasMap`` for custom column aliases.
            amount_mode: ``"signed"`` (default) or ``"debit_credit"``.
        """
        path = Path(path)
        source = read_source_file_contents(path)
        raw_bytes = source.content
        return self._parse_hardened(
            io.StringIO(raw_bytes.decode("utf-8")),
            source=str(path),
            alias_map=alias_map,
            amount_mode=amount_mode,
            source_content_hash=source.evidence.content_hash,
            source_file_path=str(path),
            source_filename=source.evidence.original_filename,
        )

    def parse_text_hardened(
        self,
        text: str,
        *,
        source: str = "<string>",
        alias_map: ColumnAliasMap | None = None,
        amount_mode: AmountMode = "signed",
    ) -> CsvImportResult:
        """Parse CSV text with the hardened parser.

        Parameters:
            text: CSV text content.
            source: Label for the source in error messages.
            alias_map: Optional ``ColumnAliasMap`` for custom column aliases.
            amount_mode: ``"signed"`` (default) or ``"debit_credit"``.
        """
        with io.StringIO(text) as fh:
            return self._parse_hardened(
                fh,
                source=source,
                alias_map=alias_map,
                amount_mode=amount_mode,
                source_content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )

    # ------------------------------------------------------------------
    # Internal: legacy parser
    # ------------------------------------------------------------------

    def _parse_legacy(
        self,
        fh: Any,
        source: str,
        *,
        source_content_hash: str | None = None,
        source_file_path: str | None = None,
        source_filename: str | None = None,
    ) -> CsvImportResult:
        """Legacy CSV parser for backward compatibility."""
        rows: list[StructuredStatementRow] = []
        errors: list[str] = []
        skipped: int = 0

        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return CsvImportResult(
                rows=[],
                error_count=1,
                errors=[f"No header row found in {source}"],
            )

        missing = [c for c in self._required if c not in reader.fieldnames]
        if missing:
            return CsvImportResult(
                rows=[],
                error_count=1,
                errors=[f"Missing required columns in {source}: {', '.join(missing)}"],
            )

        for line_num, row_dict in enumerate(reader, start=2):
            try:
                srow = _csv_row_to_structured(
                    row_dict,
                    reader.fieldnames or [],
                    line_num=line_num,
                    source_content_hash=source_content_hash,
                )
                if srow is not None:
                    rows.append(srow)
                else:
                    skipped += 1
            except (ValueError, InvalidOperation) as exc:
                errors.append(f"Line {line_num} in {source}: {exc}")

        return CsvImportResult(
            rows=rows,
            error_count=len(errors),
            skipped_count=skipped,
            errors=errors,
            source_content_hash=source_content_hash,
            source_file_path=source_file_path,
            source_filename=source_filename,
        )

    # ------------------------------------------------------------------
    # Internal: hardened parser
    # ------------------------------------------------------------------

    def _parse_hardened(
        self,
        fh: Any,
        source: str,
        alias_map: ColumnAliasMap | None = None,
        amount_mode: AmountMode = "signed",
        source_content_hash: str | None = None,
        source_file_path: str | None = None,
        source_filename: str | None = None,
    ) -> CsvImportResult:
        """Hardened CSV parser with column aliases, amount normalisation,
        date flexibility, and per-row validation errors."""
        am = alias_map or _DEFAULT_ALIAS_MAP

        if amount_mode not in ("signed", "debit_credit"):
            return CsvImportResult(
                rows=[],
                error_count=1,
                errors=[f"Unsupported amount_mode: {amount_mode!r}"],
            )

        rows: list[StructuredStatementRow] = []
        errors: list[str] = []
        validation_errors: list[RowValidationError] = []
        warnings: list[str] = []
        skipped: int = 0

        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return CsvImportResult(
                rows=[],
                error_count=1,
                errors=[f"No header row found in {source}"],
            )

        # Resolve column mapping
        canonical = _resolve_columns(reader.fieldnames, am, amount_mode)
        if canonical is None:
            return CsvImportResult(
                rows=[],
                error_count=1,
                errors=[f"Cannot resolve required columns in {source}"],
            )

        for line_num, raw_row in enumerate(reader, start=2):
            result = _csv_row_to_structured_hardened(
                raw_row,
                canonical,
                line_num=line_num,
                amount_mode=amount_mode,
                source_content_hash=source_content_hash,
            )
            if result.row is not None:
                rows.append(result.row)
            if result.skipped:
                skipped += 1
            if result.warning:
                warnings.append(f"Line {line_num} in {source}: {result.warning}")
            if result.validation_error:
                validation_errors.append(result.validation_error)

        return CsvImportResult(
            rows=rows,
            error_count=len(errors) + len(validation_errors),
            skipped_count=skipped,
            errors=errors,
            warnings=warnings,
            validation_errors=validation_errors,
            source_content_hash=source_content_hash,
            source_file_path=source_file_path,
            source_filename=source_filename,
        )


# ---------------------------------------------------------------------------
# Internal: hardened row result
# ---------------------------------------------------------------------------


@dataclass
class _HardenedRowResult:
    """Internal result of converting a single CSV row via the hardened path."""

    row: StructuredStatementRow | None = None
    skipped: bool = False
    warning: str | None = None
    validation_error: RowValidationError | None = None


# ---------------------------------------------------------------------------
# Hardened row conversion
# ---------------------------------------------------------------------------


def _csv_row_to_structured_hardened(
    row_dict: CsvRow,
    canonical: CanonicalColumnMap,
    line_num: int = 0,
    amount_mode: AmountMode = "signed",
    source_content_hash: str | None = None,
) -> _HardenedRowResult:
    """Convert a single CSV row to a ``StructuredStatementRow`` using the
    hardened parser with validation.

    Args:
        row_dict: Raw CSV row dict.
        canonical: Resolved column mapping (canonical name -> actual header).
        line_num: 1-based CSV line number for error reporting.
        amount_mode: ``"signed"`` or ``"debit_credit"``.

    Returns:
        ``_HardenedRowResult`` with the parsed row (or None) and any
        validation error or warning.
    """
    warnings: list[str] = []

    # --- Merchant ---
    merchant_raw = _get_cell(row_dict, canonical, "merchant_raw")
    if not merchant_raw:
        return _HardenedRowResult(skipped=True)

    # --- Dates ---
    txn_date_str = _get_cell(row_dict, canonical, "transaction_date")
    pst_date_str = _get_cell(row_dict, canonical, "posted_date")

    txn_date: date | None = None
    pst_date: date | None = None

    if txn_date_str:
        try:
            txn_date = _parse_date_flexible(txn_date_str)
        except ValueError as e:
            return _HardenedRowResult(
                validation_error=RowValidationError(
                    row_number=line_num,
                    raw_row=row_dict,
                    error_message=str(e),
                    field="transaction_date",
                )
            )

    if pst_date_str:
        try:
            pst_date = _parse_date_flexible(pst_date_str)
        except ValueError as e:
            return _HardenedRowResult(
                validation_error=RowValidationError(
                    row_number=line_num,
                    raw_row=row_dict,
                    error_message=str(e),
                    field="posted_date",
                )
            )

    # Both dates absent -> skip row with validation error.
    # transaction_date and posted_date are preserved independently;
    # no auto-backfill from one to the other.
    if txn_date is None and pst_date is None:
        return _HardenedRowResult(
            validation_error=RowValidationError(
                row_number=line_num,
                raw_row=row_dict,
                error_message="Missing transaction_date and posted_date",
                field="transaction_date",
            )
        )

    # --- Amount ---
    amount: Decimal | None = None
    amount_direction: StatementAmountDirection | None = None
    raw_amount: str | None = None
    raw_type: str | None = None  # optional raw type column from CSV

    # Read raw type from optional type column
    raw_type = _get_cell(row_dict, canonical, "type")

    if amount_mode == "signed":
        amount_str = _get_cell(row_dict, canonical, "amount")
        if not amount_str:
            return _HardenedRowResult(
                validation_error=RowValidationError(
                    row_number=line_num,
                    raw_row=row_dict,
                    error_message="Missing amount",
                    field="amount",
                )
            )
        try:
            parsed = _parse_signed_amount(amount_str)
        except (ValueError, InvalidOperation) as e:
            return _HardenedRowResult(
                validation_error=RowValidationError(
                    row_number=line_num,
                    raw_row=row_dict,
                    error_message=str(e),
                    field="amount",
                )
            )
        amount = parsed
        direction = classify_row_direction(
            amount_raw=amount_str,
            debit_raw=None,
            credit_raw=None,
            merchant_raw=merchant_raw,
            amount_mode=amount_mode,
            raw_type=raw_type,
            amount_parsed=parsed,
        )
        amount_direction = direction
        raw_amount = amount_str
        # Use absolute value for amount storage
        if parsed < 0:
            amount = abs(parsed)

    else:  # debit_credit mode
        debit_str = _get_cell(row_dict, canonical, "debit")
        credit_str = _get_cell(row_dict, canonical, "credit")

        debit_val: Decimal | None = None
        credit_val: Decimal | None = None

        if debit_str:
            try:
                debit_val = _normalize_amount(debit_str)
            except (ValueError, InvalidOperation) as e:
                return _HardenedRowResult(
                    validation_error=RowValidationError(
                        row_number=line_num,
                        raw_row=row_dict,
                        error_message=f"Invalid debit amount: {e}",
                        field="debit",
                    )
                )

        if credit_str:
            try:
                credit_val = _normalize_amount(credit_str)
            except (ValueError, InvalidOperation) as e:
                return _HardenedRowResult(
                    validation_error=RowValidationError(
                        row_number=line_num,
                        raw_row=row_dict,
                        error_message=f"Invalid credit amount: {e}",
                        field="credit",
                    )
                )

        if debit_val is not None and debit_val > 0 and credit_val is not None and credit_val > 0:
            # Both debit and credit present -> validation error
            return _HardenedRowResult(
                validation_error=RowValidationError(
                    row_number=line_num,
                    raw_row=row_dict,
                    error_message=(
                        "Both debit and credit amounts present; cannot determine direction"
                    ),
                    field="amount",
                )
            )

        if debit_val is not None and debit_val > 0:
            amount = debit_val  # positive spend
            direction = classify_row_direction(
                amount_raw=None,
                debit_raw=debit_str,
                credit_raw=credit_str,
                merchant_raw=merchant_raw,
                amount_mode=amount_mode,
                raw_type=raw_type,
                amount_parsed=debit_val,
            )
            amount_direction = direction
            raw_amount = debit_str
        elif credit_val is not None and credit_val > 0:
            # Credit-only row: classify and pass through
            direction = classify_row_direction(
                amount_raw=None,
                debit_raw=debit_str,
                credit_raw=credit_str,
                merchant_raw=merchant_raw,
                amount_mode=amount_mode,
                raw_type=raw_type,
                amount_parsed=credit_val,
            )
            amount_direction = direction
            raw_amount = credit_str
            amount = credit_val  # positive amount from credit column
        else:
            return _HardenedRowResult(
                validation_error=RowValidationError(
                    row_number=line_num,
                    raw_row=row_dict,
                    error_message="Missing amount (no debit or credit)",
                    field="amount",
                )
            )

    if amount is None:
        return _HardenedRowResult(
            validation_error=RowValidationError(
                row_number=line_num,
                raw_row=row_dict,
                error_message="Could not determine valid amount for row",
                field="amount",
            )
        )

    assert amount is not None

    # --- Currency ---
    currency_raw = _get_cell(row_dict, canonical, "currency")
    if not currency_raw:
        return _HardenedRowResult(
            validation_error=RowValidationError(
                row_number=line_num,
                raw_row=row_dict,
                error_message="Missing currency",
                field="currency",
            )
        )

    try:
        currency = _validate_currency(currency_raw)
    except ValueError as e:
        return _HardenedRowResult(
            validation_error=RowValidationError(
                row_number=line_num,
                raw_row=row_dict,
                error_message=str(e),
                field="currency",
            )
        )

    # --- Reference ---
    reference = _get_cell(row_dict, canonical, "reference")
    if not reference and line_num:
        reference = f"csv-line-{line_num}"

    # --- Raw row payload ---
    raw_payload: dict[str, Any] = {k: v for k, v in row_dict.items()}

    # --- Warning consolidation ---
    warning_str = "; ".join(warnings) if warnings else None

    return _HardenedRowResult(
        row=StructuredStatementRow(
            transaction_date=txn_date,
            posted_date=pst_date,
            merchant_raw=merchant_raw,
            amount=amount,
            currency=currency,
            statement_row_reference=reference,
            raw_row_payload=raw_payload,
            # The authoritative importer owns the exact generic-v1 material,
            # including import context unavailable to this parser adapter.
            row_fingerprint=None,
            row_fingerprint_version=None,
            fingerprint_source_content_hash=source_content_hash,
            amount_direction=amount_direction,
            raw_amount_type=raw_type,
            raw_amount=raw_amount,
        ),
        warning=warning_str,
    )


# ---------------------------------------------------------------------------
# Legacy row conversion (preserved for backward compatibility)
# ---------------------------------------------------------------------------


def _csv_row_to_structured(
    row_dict: CsvRow,
    fieldnames: Sequence[str],
    line_num: int = 0,
    source_content_hash: str | None = None,
) -> StructuredStatementRow | None:
    """Convert a single CSV row dict to a ``StructuredStatementRow``.

    Returns ``None`` for rows that should be silently skipped (e.g. empty
    required fields).
    """
    merchant_raw = row_dict.get("merchant_raw", "").strip()
    if not merchant_raw:
        return None

    amount_str = row_dict.get("amount", "").strip()
    if not amount_str:
        return None
    amount = Decimal(amount_str)
    if amount <= 0:
        raise ValueError(f"Amount must be positive, got {amount}")

    currency = row_dict.get("currency", "").strip()
    if not currency:
        return None

    # Dates
    txn_date = _parse_date(row_dict.get("transaction_date"))
    posted_date = _parse_date(row_dict.get("posted_date"))

    # Optional fields
    merchant_normalized = row_dict.get("merchant_normalized") or None
    account_name = row_dict.get("account_name") or None
    account_id_str = row_dict.get("account_id") or None
    account_id: str | None = account_id_str.strip() if account_id_str else None
    explicit_ref = row_dict.get("statement_row_reference") or None
    if explicit_ref:
        statement_row_reference = explicit_ref
    elif line_num:
        statement_row_reference = f"csv-line-{line_num}"
    else:
        statement_row_reference = None

    raw_row_payload: dict[str, Any] | None = None
    raw_payload_str = row_dict.get("raw_row_payload")
    if raw_payload_str and raw_payload_str.strip():
        try:
            raw_row_payload = json.loads(raw_payload_str)
        except json.JSONDecodeError:
            raw_row_payload = {"raw": raw_payload_str}

    if raw_row_payload is None:
        raw_row_payload = {k: v for k, v in row_dict.items() if k in fieldnames}

    return StructuredStatementRow(
        transaction_date=txn_date,
        posted_date=posted_date,
        merchant_raw=merchant_raw,
        merchant_normalized=merchant_normalized,
        amount=amount,
        currency=currency,
        account_name=account_name,
        account_id=account_id,
        statement_row_reference=statement_row_reference,
        raw_row_payload=raw_row_payload,
        fingerprint_source_content_hash=source_content_hash,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "StatementCsvAdapter",
    "CsvImportResult",
    "RowValidationError",
    "ColumnAliasMap",
    "_csv_row_fingerprint",
    "_parse_date_flexible",
    "_parse_signed_amount",
    "_validate_currency",
    "_normalize_amount",
    "_resolve_columns",
]
