"""PDF Statement Import Bridge v1 — contract tests.

Defines and asserts the contract that a future PDF statement parser must
satisfy before its output can enter the existing statement import and
reconciliation pipeline.

These tests:
- Use synthetic ``ParsedPdfStatementRow`` instances (no real PDFs, no OCR).
- Assert date preservation, amount normalization, currency rules,
  fingerprint stability, evidence retention, and blocked-state contracts.
- Never touch ``database/finance.db``.
- Do not depend on SQLite, migrations, or the import runtime.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.statement_identity import ROW_FINGERPRINT_VERSION
from finance_core.reconciliation.statement_import_contracts import StructuredStatementRow

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"


# ---------------------------------------------------------------------------
# Contract dataclass — the canonical parsed PDF row shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedPdfStatementRow:
    """A single parsed row from a PDF statement, before normalization.

    This is the canonical structure that a future PDF parser must
    populate.  It is read-only and carries no runtime logic.
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
    amount_direction: StatementAmountDirection = StatementAmountDirection.DEBIT


# ---------------------------------------------------------------------------
# Bridge helpers — deterministic normalization functions under contract
# ---------------------------------------------------------------------------


def _pdf_row_fingerprint(row: ParsedPdfStatementRow) -> str:
    """Generate a deterministic SHA-256 fingerprint for a parsed PDF row.

    Format: full lowercase SHA-256; the version is persisted separately.

    Fingerprint inputs are intentionally limited to materially identifying
    fields.  Volatile fields such as ``raw_row_text`` and timestamps are
    excluded so that re-parsing the same source row produces the same
    fingerprint.
    """
    txn = row.transaction_date.isoformat() if row.transaction_date else "nodate"
    pst = row.posted_date.isoformat() if row.posted_date else "nodate"
    page = str(row.source_page_number) if row.source_page_number is not None else "nopage"
    rowref = row.source_row_ref if row.source_row_ref else "norowref"
    direction = row.amount_direction.value if row.amount_direction else "nodirection"

    raw = (
        f"{row.source_statement_id}|{page}|{rowref}|"
        f"{row.description}|{txn}|{pst}|{str(row.amount)}|{row.currency}|{direction}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_pdf_amount(value: Decimal, direction: StatementAmountDirection) -> Decimal:
    """Normalize a PDF-row amount to non-negative Decimal.

    All ``StructuredStatementRow.amount`` values must be non-negative.
    The direction is carried separately via ``amount_direction``.
    """
    if value < 0:
        return abs(value)
    return value


def _build_evidence_payload(row: ParsedPdfStatementRow) -> dict[str, Any]:
    """Build the ``raw_row_payload`` evidence dict for a PDF row."""
    payload: dict[str, Any] = {
        "attachment_path": row.attachment_path,
        "raw_row_text": row.raw_row_text,
    }
    if row.source_page_number is not None:
        payload["source_page_number"] = row.source_page_number
    if row.source_row_ref is not None:
        payload["source_row_ref"] = row.source_row_ref
    return payload


def normalize_pdf_row(row: ParsedPdfStatementRow) -> StructuredStatementRow:
    """Normalize a parsed PDF row into a ``StructuredStatementRow``.

    This is the bridge contract: a future PDF parser's output, when
    passed through this function, must produce rows that are compatible
    with ``StatementImporter.import_rows()``.

    Returns a ``StructuredStatementRow`` with:
    - Non-negative amount.
    - Independent transaction_date / posted_date.
    - Amount direction classification.
    - Raw amount text preserved.
    - Source evidence in raw_row_payload.
    - Deterministic full SHA-256 row fingerprint.
    """
    normalized_amount = _normalize_pdf_amount(row.amount, row.amount_direction)
    fingerprint = _pdf_row_fingerprint(row)
    evidence = _build_evidence_payload(row)

    return StructuredStatementRow(
        merchant_raw=row.description,
        amount=normalized_amount,
        currency=row.currency,
        transaction_date=row.transaction_date,
        posted_date=row.posted_date,
        amount_direction=row.amount_direction,
        raw_amount=str(row.amount),
        raw_amount_type=row.amount_direction.value if row.amount_direction else None,
        statement_row_reference=row.source_row_ref,
        raw_row_payload=evidence,
        row_fingerprint=fingerprint,
        row_fingerprint_version=ROW_FINGERPRINT_VERSION,
    )


def _is_blocked(row: ParsedPdfStatementRow) -> tuple[bool, str]:
    """Check whether a parsed PDF row must be blocked for review.

    Returns (is_blocked, reason).
    """
    if row.amount is None:
        return True, "missing_amount"
    if row.amount_direction == StatementAmountDirection.UNKNOWN:
        return True, "ambiguous_debit_credit_sign"
    if not row.currency or len(row.currency.strip()) < 3:
        return True, "missing_currency"
    if row.transaction_date is None and row.posted_date is None:
        return True, "missing_usable_date"
    if not row.description or not row.description.strip():
        return True, "missing_description"
    return False, ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(
    source_id: str = "stmt-hash-abc123",
    attachment: str = "/tmp/stmt-jan-2025.pdf",
    description: str = "APPLE.COM/BILL",
    amount: Decimal | None = Decimal("12.34"),
    currency: str = "SGD",
    txn_date: date | None = date(2025, 1, 15),
    posted_date: date | None = date(2025, 1, 17),
    direction: StatementAmountDirection = StatementAmountDirection.DEBIT,
    page: int | None = 1,
    row_ref: str | None = "row-5",
    raw_text: str = "15/01 APPLE.COM/BILL 12.34",
) -> ParsedPdfStatementRow:
    return ParsedPdfStatementRow(
        source_statement_id=source_id,
        attachment_path=attachment,
        description=description,
        amount=amount,
        currency=currency,
        source_page_number=page,
        source_row_ref=row_ref,
        transaction_date=txn_date,
        posted_date=posted_date,
        raw_row_text=raw_text,
        amount_direction=direction,
    )


# ===================================================================
# 1. Date preservation
# ===================================================================


class TestDatePreservation:
    """Both transaction_date and posted_date are preserved independently."""

    def test_both_dates_preserved(self) -> None:
        row = _make_row(
            txn_date=date(2025, 3, 10),
            posted_date=date(2025, 3, 12),
        )
        result = normalize_pdf_row(row)
        assert result.transaction_date == date(2025, 3, 10)
        assert result.posted_date == date(2025, 3, 12)

    def test_posted_date_only_does_not_fabricate_transaction_date(self) -> None:
        row = _make_row(
            txn_date=None,
            posted_date=date(2025, 3, 12),
        )
        result = normalize_pdf_row(row)
        assert result.transaction_date is None
        assert result.posted_date == date(2025, 3, 12)

    def test_transaction_date_only_does_not_fabricate_posted_date(self) -> None:
        row = _make_row(
            txn_date=date(2025, 3, 10),
            posted_date=None,
        )
        result = normalize_pdf_row(row)
        assert result.transaction_date == date(2025, 3, 10)
        assert result.posted_date is None

    def test_neither_date_is_blocked(self) -> None:
        row = _make_row(txn_date=None, posted_date=None)
        blocked, reason = _is_blocked(row)
        assert blocked
        assert reason == "missing_usable_date"

    def test_posted_date_not_collapsed_into_transaction_date(self) -> None:
        """When only posted_date exists, txn_date stays None."""
        row = _make_row(txn_date=None, posted_date=date(2025, 3, 12))
        result = normalize_pdf_row(row)
        assert result.transaction_date is None
        assert result.posted_date is not None


# ===================================================================
# 2. Amount sign normalization
# ===================================================================


class TestAmountNormalization:
    """Debit/credit signs are normalized deterministically."""

    def test_debit_row_positive(self) -> None:
        row = _make_row(amount=Decimal("45.00"), direction=StatementAmountDirection.DEBIT)
        result = normalize_pdf_row(row)
        assert result.amount == Decimal("45.00")
        assert result.amount_direction == StatementAmountDirection.DEBIT

    def test_credit_row_positive_after_normalization(self) -> None:
        row = _make_row(amount=Decimal("200.00"), direction=StatementAmountDirection.CREDIT)
        result = normalize_pdf_row(row)
        assert result.amount == Decimal("200.00")
        assert result.amount_direction == StatementAmountDirection.CREDIT

    def test_negative_amount_becomes_positive(self) -> None:
        row = _make_row(amount=Decimal("-12.34"), direction=StatementAmountDirection.CREDIT)
        result = normalize_pdf_row(row)
        assert result.amount == Decimal("12.34")
        # direction is preserved regardless of sign normalization
        assert result.amount_direction == StatementAmountDirection.CREDIT

    def test_refund_row_preserves_direction(self) -> None:
        row = _make_row(amount=Decimal("18.50"), direction=StatementAmountDirection.REFUND)
        result = normalize_pdf_row(row)
        assert result.amount_direction == StatementAmountDirection.REFUND
        assert result.amount == Decimal("18.50")

    def test_payment_row_preserves_direction(self) -> None:
        row = _make_row(
            amount=Decimal("500.00"),
            direction=StatementAmountDirection.PAYMENT,
            description="PAYMENT THANK YOU",
        )
        result = normalize_pdf_row(row)
        assert result.amount_direction == StatementAmountDirection.PAYMENT
        assert result.amount == Decimal("500.00")

    def test_fee_row_preserves_direction(self) -> None:
        row = _make_row(
            amount=Decimal("20.00"),
            direction=StatementAmountDirection.FEE,
            description="ANNUAL FEE",
        )
        result = normalize_pdf_row(row)
        assert result.amount_direction == StatementAmountDirection.FEE

    def test_unknown_direction_blocks_row(self) -> None:
        row = _make_row(amount=Decimal("10.00"), direction=StatementAmountDirection.UNKNOWN)
        blocked, reason = _is_blocked(row)
        assert blocked
        assert reason == "ambiguous_debit_credit_sign"

    def test_raw_amount_preserved(self) -> None:
        row = _make_row(amount=Decimal("12.34"))
        result = normalize_pdf_row(row)
        assert result.raw_amount == "12.34"

    def test_negative_raw_amount_preserved_as_original(self) -> None:
        row = _make_row(amount=Decimal("-12.34"))
        result = normalize_pdf_row(row)
        # raw_amount preserves original value; amount is normalized
        assert result.raw_amount == "-12.34"
        assert result.amount == Decimal("12.34")


# ===================================================================
# 3. Currency
# ===================================================================


class TestCurrency:
    """Currency must be explicit; missing/ambiguous currency blocks the row."""

    def test_explicit_currency_preserved(self) -> None:
        row = _make_row(currency="USD")
        result = normalize_pdf_row(row)
        assert result.currency == "USD"

    def test_missing_currency_blocks(self) -> None:
        row = _make_row(currency="")
        blocked, reason = _is_blocked(row)
        assert blocked
        assert reason == "missing_currency"

    def test_short_currency_blocks(self) -> None:
        row = _make_row(currency="S")
        blocked, reason = _is_blocked(row)
        assert blocked
        assert reason == "missing_currency"

    def test_none_currency_blocks(self) -> None:
        row = _make_row(currency="")
        blocked, reason = _is_blocked(row)
        assert blocked


# ===================================================================
# 4. Fingerprint stability
# ===================================================================


class TestFingerprint:
    """PDF row fingerprints are deterministic and stable."""

    def test_identical_rows_same_fingerprint(self) -> None:
        a = _make_row(
            source_id="hash-1",
            txn_date=date(2025, 2, 1),
            amount=Decimal("9.90"),
            currency="SGD",
        )
        b = _make_row(
            source_id="hash-1",
            txn_date=date(2025, 2, 1),
            amount=Decimal("9.90"),
            currency="SGD",
        )
        assert _pdf_row_fingerprint(a) == _pdf_row_fingerprint(b)

    def test_different_source_changes_fingerprint(self) -> None:
        a = _make_row(source_id="hash-1")
        b = _make_row(source_id="hash-2")
        assert _pdf_row_fingerprint(a) != _pdf_row_fingerprint(b)

    def test_different_page_changes_fingerprint(self) -> None:
        a = _make_row(page=1)
        b = _make_row(page=2)
        assert _pdf_row_fingerprint(a) != _pdf_row_fingerprint(b)

    def test_different_row_ref_changes_fingerprint(self) -> None:
        a = _make_row(row_ref="row-1")
        b = _make_row(row_ref="row-2")
        assert _pdf_row_fingerprint(a) != _pdf_row_fingerprint(b)

    def test_different_date_changes_fingerprint(self) -> None:
        a = _make_row(txn_date=date(2025, 1, 1))
        b = _make_row(txn_date=date(2025, 1, 2))
        assert _pdf_row_fingerprint(a) != _pdf_row_fingerprint(b)

    def test_different_amount_changes_fingerprint(self) -> None:
        a = _make_row(amount=Decimal("10.00"))
        b = _make_row(amount=Decimal("20.00"))
        assert _pdf_row_fingerprint(a) != _pdf_row_fingerprint(b)

    def test_different_currency_changes_fingerprint(self) -> None:
        a = _make_row(currency="SGD")
        b = _make_row(currency="USD")
        assert _pdf_row_fingerprint(a) != _pdf_row_fingerprint(b)

    def test_different_amount_direction_changes_fingerprint(self) -> None:
        a = _make_row(amount=Decimal("10.00"), direction=StatementAmountDirection.DEBIT)
        b = _make_row(amount=Decimal("10.00"), direction=StatementAmountDirection.CREDIT)
        assert _pdf_row_fingerprint(a) != _pdf_row_fingerprint(b)

    def test_different_description_changes_fingerprint(self) -> None:
        a = _make_row(description="APPLE")
        b = _make_row(description="GOOGLE")
        assert _pdf_row_fingerprint(a) != _pdf_row_fingerprint(b)

    def test_full_sha256_fingerprint(self) -> None:
        row = _make_row()
        fp = _pdf_row_fingerprint(row)
        assert len(fp) == 64
        int(fp, 16)

    def test_fingerprint_excludes_raw_row_text(self) -> None:
        """Volatile raw_row_text must not affect fingerprint stability."""
        a = _make_row(raw_text="15/01 APPLE 12.34")
        b = _make_row(raw_text="15/01 APPLE 12.34  (slightly different format)")
        assert _pdf_row_fingerprint(a) == _pdf_row_fingerprint(b)

    def test_fingerprint_format_stable(self) -> None:
        """Fingerprint format is a full lowercase SHA-256 digest."""
        row = _make_row()
        fp = _pdf_row_fingerprint(row)
        assert len(fp) == 64
        assert fp == fp.lower()
        int(fp, 16)


# ===================================================================
# 5. Evidence preservation
# ===================================================================


class TestEvidencePreservation:
    """Source evidence from the PDF is preserved through normalization."""

    def test_attachment_path_in_evidence(self) -> None:
        row = _make_row(attachment="/data/stmts/jan.pdf")
        result = normalize_pdf_row(row)
        assert result.raw_row_payload is not None
        assert result.raw_row_payload["attachment_path"] == "/data/stmts/jan.pdf"

    def test_page_number_in_evidence(self) -> None:
        row = _make_row(page=3)
        result = normalize_pdf_row(row)
        assert result.raw_row_payload is not None
        assert result.raw_row_payload["source_page_number"] == 3

    def test_row_ref_in_evidence(self) -> None:
        row = _make_row(row_ref="row-7")
        result = normalize_pdf_row(row)
        assert result.raw_row_payload is not None
        assert result.raw_row_payload["source_row_ref"] == "row-7"

    def test_raw_text_in_evidence(self) -> None:
        row = _make_row(raw_text="10/02 NETFLIX 15.98 SGD")
        result = normalize_pdf_row(row)
        assert result.raw_row_payload is not None
        assert result.raw_row_payload["raw_row_text"] == "10/02 NETFLIX 15.98 SGD"

    def test_missing_page_not_in_evidence(self) -> None:
        row = _make_row(page=None)
        result = normalize_pdf_row(row)
        assert result.raw_row_payload is not None
        assert "source_page_number" not in result.raw_row_payload

    def test_null_row_ref_not_in_evidence(self) -> None:
        row = _make_row(row_ref=None)
        result = normalize_pdf_row(row)
        assert result.raw_row_payload is not None
        assert "source_row_ref" not in result.raw_row_payload

    def test_statement_row_reference_set_from_row_ref(self) -> None:
        row = _make_row(row_ref="row-7")
        result = normalize_pdf_row(row)
        assert result.statement_row_reference == "row-7"


# ===================================================================
# 6. Blocked states
# ===================================================================


class TestBlockedStates:
    """Contract-level blocked/needs-review states are enforced."""

    def test_missing_amount_blocks(self) -> None:
        row = _make_row(amount=None)
        blocked, reason = _is_blocked(row)
        assert blocked
        assert reason == "missing_amount"

    def test_missing_currency_blocks(self) -> None:
        row = _make_row(currency="")
        blocked, reason = _is_blocked(row)
        assert blocked
        assert "missing_currency" in reason

    def test_missing_usable_date_blocks(self) -> None:
        row = _make_row(txn_date=None, posted_date=None)
        blocked, reason = _is_blocked(row)
        assert blocked
        assert reason == "missing_usable_date"

    def test_missing_description_blocks(self) -> None:
        row = _make_row(description="")
        blocked, reason = _is_blocked(row)
        assert blocked
        assert reason == "missing_description"

    def test_unknown_direction_blocks(self) -> None:
        row = _make_row(direction=StatementAmountDirection.UNKNOWN)
        blocked, reason = _is_blocked(row)
        assert blocked
        assert reason == "ambiguous_debit_credit_sign"

    def test_complete_row_not_blocked(self) -> None:
        row = _make_row()
        blocked, reason = _is_blocked(row)
        assert not blocked, f"Expected not blocked, got {reason!r}"


# ===================================================================
# 7. Normalized output contract (StructuredStatementRow compatibility)
# ===================================================================


class TestNormalizedOutput:
    """normalize_pdf_row produces valid StructuredStatementRow instances."""

    def test_output_is_structured_statement_row(self) -> None:
        row = _make_row()
        result = normalize_pdf_row(row)
        assert isinstance(result, StructuredStatementRow)

    def test_merchant_raw_from_description(self) -> None:
        row = _make_row(description="NETFLIX")
        result = normalize_pdf_row(row)
        assert result.merchant_raw == "NETFLIX"

    def test_amount_non_negative(self) -> None:
        row = _make_row(amount=Decimal("12.34"))
        result = normalize_pdf_row(row)
        assert result.amount >= 0

    def test_amount_non_negative_after_negative_input(self) -> None:
        row = _make_row(amount=Decimal("-5.00"))
        result = normalize_pdf_row(row)
        assert result.amount >= 0
        assert result.amount == Decimal("5.00")

    def test_currency_preserved(self) -> None:
        row = _make_row(currency="EUR")
        result = normalize_pdf_row(row)
        assert result.currency == "EUR"

    def test_fingerprint_populated(self) -> None:
        row = _make_row()
        result = normalize_pdf_row(row)
        assert result.row_fingerprint is not None
        assert len(result.row_fingerprint) == 64
        assert result.row_fingerprint_version == ROW_FINGERPRINT_VERSION

    def test_amount_direction_preserved(self) -> None:
        for direction in StatementAmountDirection:
            row = _make_row(direction=direction)
            result = normalize_pdf_row(row)
            assert result.amount_direction == direction


# ===================================================================
# 8. Safety — no live DB access
# ===================================================================


class TestNoLiveDbAccess:
    """None of the contract helpers touch database/finance.db."""

    def test_normalize_does_not_touch_live_db(self) -> None:
        row = _make_row()
        # This would fail if normalize_pdf_row tried to open LIVE_DB_PATH
        result = normalize_pdf_row(row)
        assert result is not None

    def test_fingerprint_does_not_touch_live_db(self) -> None:
        row = _make_row()
        fp = _pdf_row_fingerprint(row)
        assert len(fp) == 64
        int(fp, 16)

    def test_blocked_check_does_not_touch_live_db(self) -> None:
        row = _make_row()
        _is_blocked(row)
        # No exception = no DB access


# ===================================================================
# 9. Amount direction classification — all enum variants
# ===================================================================


class TestAmountDirectionRoundTrip:
    """Every StatementAmountDirection variant survives normalization."""

    def test_debit_survives(self) -> None:
        row = _make_row(direction=StatementAmountDirection.DEBIT)
        assert normalize_pdf_row(row).amount_direction == StatementAmountDirection.DEBIT

    def test_credit_survives(self) -> None:
        row = _make_row(direction=StatementAmountDirection.CREDIT)
        assert normalize_pdf_row(row).amount_direction == StatementAmountDirection.CREDIT

    def test_refund_survives(self) -> None:
        row = _make_row(direction=StatementAmountDirection.REFUND)
        assert normalize_pdf_row(row).amount_direction == StatementAmountDirection.REFUND

    def test_reversal_survives(self) -> None:
        row = _make_row(direction=StatementAmountDirection.REVERSAL)
        assert normalize_pdf_row(row).amount_direction == StatementAmountDirection.REVERSAL

    def test_payment_survives(self) -> None:
        row = _make_row(direction=StatementAmountDirection.PAYMENT)
        assert normalize_pdf_row(row).amount_direction == StatementAmountDirection.PAYMENT

    def test_fee_survives(self) -> None:
        row = _make_row(direction=StatementAmountDirection.FEE)
        assert normalize_pdf_row(row).amount_direction == StatementAmountDirection.FEE

    def test_interest_survives(self) -> None:
        row = _make_row(direction=StatementAmountDirection.INTEREST)
        assert normalize_pdf_row(row).amount_direction == StatementAmountDirection.INTEREST
