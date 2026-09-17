"""Tests for PDF Statement Import Review Fixture v1.

Verifies that the review fixture is deterministic, read-only, correctly
separates dashboard-safe from audit payloads, preserves ordering, and
does not expose sensitive fields in dashboard payloads. Uses synthetic
data only — no real PDFs, no OCR, no live DB.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import (
    ParsedPdfStatementRow,
    PdfBlockedReason,
    normalize_pdf_statement_rows_batch,
)
from finance_core.reconciliation.pdf_statement_evidence import (
    PdfDirectionConfidence,
    PdfDirectionSource,
    PdfOriginalAmountSign,
    PdfRowReviewStatus,
)
from finance_core.reconciliation.pdf_statement_import_review_fixture import (
    PdfStatementImportReviewAcceptedRow,
    PdfStatementImportReviewBlockedRow,
    PdfStatementImportReviewFixture,
    build_pdf_statement_import_review_fixture,
    export_pdf_statement_import_review_audit_payload,
    export_pdf_statement_import_review_dashboard_payload,
)

# ---------------------------------------------------------------------------
# Synthetic row factory
# ---------------------------------------------------------------------------


def _make_row(
    *,
    source_statement_id: str = "stmt-001",
    attachment_path: str = "/tmp/fake.pdf",
    description: str = "Test Merchant",
    amount: Decimal | None = Decimal("100.00"),
    currency: str = "SGD",
    source_page_number: int | None = 1,
    source_row_ref: str | None = "page-1:line-1",
    transaction_date: date | None = date(2026, 6, 1),
    posted_date: date | None = None,
    raw_row_text: str = "01/06/2026 Test Merchant 100.00 D",
    amount_direction: StatementAmountDirection = StatementAmountDirection.DEBIT,
) -> ParsedPdfStatementRow:
    token = str(amount) if amount is not None else None
    sign = (
        PdfOriginalAmountSign.MISSING
        if amount is None
        else PdfOriginalAmountSign.ZERO
        if amount == 0
        else PdfOriginalAmountSign.NEGATIVE
        if amount < 0
        else PdfOriginalAmountSign.POSITIVE
    )
    return ParsedPdfStatementRow(
        source_statement_id=source_statement_id,
        attachment_path=attachment_path,
        description=description,
        amount=amount,
        currency=currency,
        source_page_number=source_page_number,
        source_row_ref=source_row_ref,
        transaction_date=transaction_date,
        posted_date=posted_date,
        raw_row_text=raw_row_text,
        amount_direction=amount_direction,
        source_content_hash="a" * 64,
        source_row_number=1,
        source_text_excerpt=raw_row_text,
        direction_source=PdfDirectionSource.EXPLICIT_TOKEN,
        direction_confidence=PdfDirectionConfidence.HIGH,
        review_status=PdfRowReviewStatus.AUTHORITATIVE,
        original_amount_token=token,
        original_amount_sign=sign,
        currency_token=currency,
        transaction_date_token=transaction_date.isoformat() if transaction_date else None,
        posted_date_token=posted_date.isoformat() if posted_date else None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_rows(n: int = 2) -> list[ParsedPdfStatementRow]:
    return [
        _make_row(
            source_statement_id=f"s{i}",
            description=f"Merchant {i}",
            amount=Decimal(str(i * 10) + ".00"),
            transaction_date=date(2026, 7, i),
            source_page_number=i,
            source_row_ref=f"r{i}",
        )
        for i in range(1, n + 1)
    ]


def _blocked_rows(n: int = 2) -> list[ParsedPdfStatementRow]:
    return [
        _make_row(
            source_statement_id=f"sb{i}",
            description=f"Blocked {i}",
            amount=None,
            transaction_date=date(2026, 7, i),
            source_page_number=i,
            source_row_ref=f"rb{i}",
            raw_row_text=f"raw blocked {i}",
        )
        for i in range(1, n + 1)
    ]


# ===================================================================
# 1. Empty batch
# ===================================================================


class TestEmptyBatch:
    """Review fixture for empty batch must produce empty results."""

    def test_empty_batch_fixture(self) -> None:
        batch = normalize_pdf_statement_rows_batch([])
        fixture = build_pdf_statement_import_review_fixture(batch)
        assert isinstance(fixture, PdfStatementImportReviewFixture)
        assert fixture.accepted_rows == ()
        assert fixture.blocked_rows == ()
        assert fixture.summary.total_rows == 0
        assert fixture.summary.accepted_count == 0
        assert fixture.summary.blocked_count == 0
        assert fixture.summary.has_blocked_rows is False
        assert fixture.summary.blocked_reason_counts == ()

    def test_empty_batch_dashboard_payload(self) -> None:
        batch = normalize_pdf_statement_rows_batch([])
        fixture = build_pdf_statement_import_review_fixture(batch)
        payload = export_pdf_statement_import_review_dashboard_payload(fixture)
        assert payload["accepted"] == []
        assert payload["blocked"] == []
        assert payload["summary"]["total_rows"] == 0

    def test_empty_batch_audit_payload(self) -> None:
        batch = normalize_pdf_statement_rows_batch([])
        payload = export_pdf_statement_import_review_audit_payload(batch)
        assert payload["accepted"] == []
        assert payload["blocked"] == []
        assert payload["summary"]["total_rows"] == 0


# ===================================================================
# 2. All accepted
# ===================================================================


class TestAllAccepted:
    """When all rows are valid, fixture must contain only accepted rows."""

    def test_all_accepted_fixture(self) -> None:
        rows = _valid_rows(3)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        assert fixture.summary.total_rows == 3
        assert fixture.summary.accepted_count == 3
        assert fixture.summary.blocked_count == 0
        assert fixture.summary.has_blocked_rows is False
        assert len(fixture.accepted_rows) == 3
        assert fixture.blocked_rows == ()

    def test_accepted_row_fields(self) -> None:
        rows = _valid_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        row = fixture.accepted_rows[0]
        assert isinstance(row, PdfStatementImportReviewAcceptedRow)
        assert row.merchant_raw == "Merchant 1"
        assert row.amount == Decimal("10.00")
        assert row.currency == "SGD"
        assert row.transaction_date == date(2026, 7, 1)
        assert row.amount_direction == StatementAmountDirection.DEBIT
        assert len(row.row_fingerprint) == 64
        int(row.row_fingerprint, 16)

    def test_accepted_rows_preserve_order(self) -> None:
        rows = _valid_rows(3)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        assert fixture.accepted_rows[0].merchant_raw == "Merchant 1"
        assert fixture.accepted_rows[1].merchant_raw == "Merchant 2"
        assert fixture.accepted_rows[2].merchant_raw == "Merchant 3"


# ===================================================================
# 3. All blocked
# ===================================================================


class TestAllBlocked:
    """When all rows are blocked, fixture must contain only blocked rows."""

    def test_all_blocked_fixture(self) -> None:
        rows = _blocked_rows(2)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        assert fixture.summary.total_rows == 2
        assert fixture.summary.accepted_count == 0
        assert fixture.summary.blocked_count == 2
        assert fixture.summary.has_blocked_rows is True
        assert fixture.accepted_rows == ()
        assert len(fixture.blocked_rows) == 2

    def test_blocked_row_fields(self) -> None:
        rows = _blocked_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        row = fixture.blocked_rows[0]
        assert isinstance(row, PdfStatementImportReviewBlockedRow)
        assert row.source_statement_id == "sb1"
        assert row.description == "Blocked 1"
        assert PdfBlockedReason.MISSING_AMOUNT in row.blocked_reasons
        assert row.source_page_number == 1
        assert row.source_row_ref == "rb1"

    def test_blocked_rows_preserve_order(self) -> None:
        rows = _blocked_rows(3)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        assert fixture.blocked_rows[0].description == "Blocked 1"
        assert fixture.blocked_rows[1].description == "Blocked 2"
        assert fixture.blocked_rows[2].description == "Blocked 3"


# ===================================================================
# 4. Mixed
# ===================================================================


class TestMixed:
    """Mixed valid + blocked rows must separate correctly."""

    def test_mixed_separation(self) -> None:
        rows = [
            _make_row(
                description="Valid A", amount=Decimal("10.00"), transaction_date=date(2026, 7, 1)
            ),
            _make_row(description="Blocked A", amount=None, transaction_date=date(2026, 7, 1)),
            _make_row(
                description="Valid B", amount=Decimal("20.00"), transaction_date=date(2026, 7, 2)
            ),
            _make_row(description="Blocked B", amount=None, transaction_date=date(2026, 7, 2)),
        ]
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        assert fixture.summary.total_rows == 4
        assert fixture.summary.accepted_count == 2
        assert fixture.summary.blocked_count == 2
        assert fixture.summary.has_blocked_rows is True
        assert fixture.accepted_rows[0].merchant_raw == "Valid A"
        assert fixture.accepted_rows[1].merchant_raw == "Valid B"
        assert fixture.blocked_rows[0].description == "Blocked A"
        assert fixture.blocked_rows[1].description == "Blocked B"

    def test_mixed_blocked_reason_counts(self) -> None:
        rows = [
            _make_row(
                description="Valid A", amount=Decimal("10.00"), transaction_date=date(2026, 7, 1)
            ),
            _make_row(description="Blocked A", amount=None, transaction_date=date(2026, 7, 1)),
            _make_row(description="Blocked B", amount=None, transaction_date=date(2026, 7, 2)),
        ]
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        counts = fixture.summary.blocked_reason_counts
        assert (PdfBlockedReason.MISSING_AMOUNT, 2) in counts


# ===================================================================
# 5. Dashboard payload safety
# ===================================================================


class TestDashboardPayloadSafety:
    """Dashboard payload must not expose attachment_path or raw_row_text."""

    def test_dashboard_payload_excludes_attachment_path(self) -> None:
        rows = _valid_rows(1) + _blocked_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        payload = export_pdf_statement_import_review_dashboard_payload(fixture)
        payload_str = json.dumps(payload, default=str)
        assert "attachment_path" not in payload_str
        assert "/tmp/fake.pdf" not in payload_str

    def test_dashboard_payload_excludes_raw_row_text(self) -> None:
        rows = _blocked_rows(2)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        payload = export_pdf_statement_import_review_dashboard_payload(fixture)
        payload_str = json.dumps(payload, default=str)
        assert "raw_row_text" not in payload_str
        assert "raw blocked" not in payload_str

    def test_dashboard_payload_excludes_raw_row_payload(self) -> None:
        rows = _valid_rows(2)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        payload = export_pdf_statement_import_review_dashboard_payload(fixture)
        payload_str = json.dumps(payload, default=str)
        assert "raw_row_payload" not in payload_str

    def test_dashboard_payload_json_serializable(self) -> None:
        rows = _valid_rows(2) + _blocked_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        payload = export_pdf_statement_import_review_dashboard_payload(fixture)
        dumped = json.dumps(payload, default=str)
        loaded = json.loads(dumped)
        assert loaded["summary"]["total_rows"] == 3

    def test_dashboard_payload_deterministic(self) -> None:
        rows = _valid_rows(2) + _blocked_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        p1 = export_pdf_statement_import_review_dashboard_payload(fixture)
        p2 = export_pdf_statement_import_review_dashboard_payload(fixture)
        assert p1 == p2


# ===================================================================
# 6. Audit payload
# ===================================================================


class TestAuditPayload:
    """Audit payload must include source evidence for traceability."""

    def test_audit_payload_includes_attachment_path_on_blocked(self) -> None:
        rows = _blocked_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        payload = export_pdf_statement_import_review_audit_payload(batch)
        assert payload["blocked"][0]["attachment_path"] == "/tmp/fake.pdf"

    def test_audit_payload_includes_raw_row_text_on_blocked(self) -> None:
        rows = _blocked_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        payload = export_pdf_statement_import_review_audit_payload(batch)
        assert payload["blocked"][0]["raw_row_text"] == "raw blocked 1"

    def test_audit_payload_includes_raw_row_payload_on_accepted(self) -> None:
        rows = _valid_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        payload = export_pdf_statement_import_review_audit_payload(batch)
        assert "raw_row_payload" in payload["accepted"][0]
        assert payload["accepted"][0]["raw_row_payload"]["attachment_path"] == "/tmp/fake.pdf"

    def test_audit_payload_json_serializable(self) -> None:
        rows = _valid_rows(1) + _blocked_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        payload = export_pdf_statement_import_review_audit_payload(batch)
        dumped = json.dumps(payload, default=str)
        loaded = json.loads(dumped)
        assert loaded["summary"]["total_rows"] == 2

    def test_audit_payload_deterministic(self) -> None:
        rows = _valid_rows(1) + _blocked_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        p1 = export_pdf_statement_import_review_audit_payload(batch)
        p2 = export_pdf_statement_import_review_audit_payload(batch)
        assert p1 == p2


# ===================================================================
# 7. Summary
# ===================================================================


class TestSummary:
    """Summary fields must match the batch result."""

    def test_summary_total_rows(self) -> None:
        batch = normalize_pdf_statement_rows_batch(_valid_rows(5))
        fixture = build_pdf_statement_import_review_fixture(batch)
        assert fixture.summary.total_rows == 5

    def test_summary_counts_consistent(self) -> None:
        rows = _valid_rows(3) + _blocked_rows(2)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        assert fixture.summary.accepted_count == 3
        assert fixture.summary.blocked_count == 2
        assert (
            fixture.summary.accepted_count + fixture.summary.blocked_count
            == fixture.summary.total_rows
        )

    def test_summary_has_blocked_rows_true(self) -> None:
        rows = _valid_rows(1) + _blocked_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        assert fixture.summary.has_blocked_rows is True

    def test_summary_blocked_reason_counts(self) -> None:
        rows = [
            _make_row(amount=None, transaction_date=date(2026, 7, 1)),
            _make_row(amount=None, transaction_date=date(2026, 7, 1)),
        ]
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        counts = fixture.summary.blocked_reason_counts
        assert isinstance(counts, tuple)
        assert len(counts) >= 1


# ===================================================================
# 8. Deterministic ordering
# ===================================================================


class TestDeterministicOrdering:
    """Ordering must be deterministic and preserve input order."""

    def test_accepted_order_is_stable(self) -> None:
        rows = _valid_rows(5)
        batch = normalize_pdf_statement_rows_batch(rows)
        f1 = build_pdf_statement_import_review_fixture(batch)
        f2 = build_pdf_statement_import_review_fixture(batch)
        for a, b in zip(f1.accepted_rows, f2.accepted_rows):
            assert a.merchant_raw == b.merchant_raw

    def test_blocked_order_is_stable(self) -> None:
        rows = _blocked_rows(5)
        batch = normalize_pdf_statement_rows_batch(rows)
        f1 = build_pdf_statement_import_review_fixture(batch)
        f2 = build_pdf_statement_import_review_fixture(batch)
        for a, b in zip(f1.blocked_rows, f2.blocked_rows):
            assert a.description == b.description


# ===================================================================
# 9. Immutability
# ===================================================================


class TestImmutability:
    """All exported dataclasses must be frozen."""

    def test_accepted_row_is_frozen(self) -> None:
        row = _valid_rows(1)
        batch = normalize_pdf_statement_rows_batch(row)
        fixture = build_pdf_statement_import_review_fixture(batch)
        ar = fixture.accepted_rows[0]
        with pytest.raises(Exception):
            ar.merchant_raw = "changed"  # type: ignore[misc]

    def test_blocked_row_is_frozen(self) -> None:
        row = _blocked_rows(1)
        batch = normalize_pdf_statement_rows_batch(row)
        fixture = build_pdf_statement_import_review_fixture(batch)
        br = fixture.blocked_rows[0]
        with pytest.raises(Exception):
            br.description = "changed"  # type: ignore[misc]

    def test_summary_is_frozen(self) -> None:
        batch = normalize_pdf_statement_rows_batch(_valid_rows(1))
        fixture = build_pdf_statement_import_review_fixture(batch)
        with pytest.raises(Exception):
            fixture.summary.total_rows = 99  # type: ignore[misc]

    def test_fixture_is_frozen(self) -> None:
        batch = normalize_pdf_statement_rows_batch(_valid_rows(1))
        fixture = build_pdf_statement_import_review_fixture(batch)
        with pytest.raises(Exception):
            fixture.accepted_rows = ()  # type: ignore[misc]


# ===================================================================
# 10. No live DB reference
# ===================================================================


class TestNoLiveDatabaseReference:
    """Module must not reference database/finance.db or sqlite3."""

    def test_no_live_db_path_in_module(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_import_review_fixture as psirf

        source = inspect.getsource(psirf)
        assert "finance.db" not in source

    def test_no_sqlite3_import(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_import_review_fixture as psirf

        source = inspect.getsource(psirf)
        assert "sqlite3" not in source

    def test_no_pdf_ocr_dependencies(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_import_review_fixture as psirf

        source = inspect.getsource(psirf)
        source_lower = source.lower()
        for dep in (
            "pdfplumber",
            "pytesseract",
            "pypdf",
            "camelot",
            "tabula",
            "open(",
        ):
            assert dep not in source_lower, f"Unexpected dependency: {dep}"
        # "ocr" may appear in docstring non-goals — use import patterns instead.
        assert "import ocr" not in source_lower
        assert "from ocr" not in source_lower

    def test_no_file_io(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_import_review_fixture as psirf

        source = inspect.getsource(psirf)
        assert "open(" not in source


# ===================================================================
# 11. Dashboard-safe property
# ===================================================================


class TestDashboardSafe:
    """Fixture dashboard_safe property must be True."""

    def test_dashboard_safe_is_true(self) -> None:
        batch = normalize_pdf_statement_rows_batch(_valid_rows(1))
        fixture = build_pdf_statement_import_review_fixture(batch)
        assert fixture.dashboard_safe is True

    def test_dashboard_safe_with_blocked_is_true(self) -> None:
        batch = normalize_pdf_statement_rows_batch(_blocked_rows(1))
        fixture = build_pdf_statement_import_review_fixture(batch)
        assert fixture.dashboard_safe is True


# ===================================================================
# 12. Integration with normalize_pdf_statement_rows_batch
# ===================================================================


class TestIntegration:
    """End-to-end integration from batch normalization through review fixture."""

    def test_full_pipeline(self) -> None:
        rows = [
            _make_row(
                source_statement_id="stmt-a",
                description="Coffee Shop",
                amount=Decimal("5.50"),
                transaction_date=date(2026, 7, 3),
                posted_date=date(2026, 7, 4),
                currency="MYR",
                source_page_number=1,
                source_row_ref="r1",
                amount_direction=StatementAmountDirection.DEBIT,
            ),
            _make_row(
                source_statement_id="stmt-a",
                description="Unknown Charge",
                amount=None,
                transaction_date=date(2026, 7, 3),
                source_page_number=1,
                source_row_ref="r2",
                raw_row_text="??? unknown line",
            ),
            _make_row(
                source_statement_id="stmt-a",
                description="Refund",
                amount=Decimal("20.00"),
                transaction_date=date(2026, 7, 3),
                currency="MYR",
                source_page_number=2,
                source_row_ref="r3",
                amount_direction=StatementAmountDirection.REFUND,
            ),
        ]
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)

        # Accepted rows
        assert len(fixture.accepted_rows) == 2
        assert fixture.accepted_rows[0].merchant_raw == "Coffee Shop"
        assert fixture.accepted_rows[0].amount == Decimal("5.50")
        assert fixture.accepted_rows[0].currency == "MYR"
        assert fixture.accepted_rows[1].merchant_raw == "Refund"
        assert fixture.accepted_rows[1].amount_direction == StatementAmountDirection.REFUND

        # Blocked rows
        assert len(fixture.blocked_rows) == 1
        assert fixture.blocked_rows[0].description == "Unknown Charge"
        assert PdfBlockedReason.MISSING_AMOUNT in fixture.blocked_rows[0].blocked_reasons

        # Summary
        assert fixture.summary.total_rows == 3
        assert fixture.summary.accepted_count == 2
        assert fixture.summary.blocked_count == 1
        assert fixture.summary.has_blocked_rows is True

    def test_dashboard_and_audit_separation(self) -> None:
        rows = _valid_rows(1) + _blocked_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)

        dash = export_pdf_statement_import_review_dashboard_payload(fixture)
        audit = export_pdf_statement_import_review_audit_payload(batch)

        # Dashboard must not have attachment_path anywhere
        dash_str = json.dumps(dash, default=str)
        assert "attachment_path" not in dash_str
        assert "raw_row_text" not in dash_str

        # Audit must have attachment_path on blocked rows
        assert "attachment_path" in audit["blocked"][0]
        assert audit["blocked"][0]["attachment_path"] == "/tmp/fake.pdf"


# ===================================================================
# 13. Field completeness
# ===================================================================


class TestFieldCompleteness:
    """Accepted and blocked rows must carry all expected fields."""

    def test_accepted_row_has_all_expected_fields(self) -> None:
        rows = _valid_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        row = fixture.accepted_rows[0]
        assert row.merchant_raw is not None
        assert row.amount is not None
        assert row.currency is not None
        assert row.amount_direction is not None
        assert row.row_fingerprint is not None

    def test_blocked_row_has_all_expected_fields(self) -> None:
        rows = _blocked_rows(1)
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        row = fixture.blocked_rows[0]
        assert row.source_statement_id is not None
        assert row.description is not None
        assert row.blocked_reasons != ()
        assert row.row_fingerprint is not None

    def test_blocked_row_excludes_attachment_path(self) -> None:
        """The review blocked row dataclass must not even have an
        attachment_path field."""
        assert not hasattr(PdfStatementImportReviewBlockedRow, "attachment_path")

    def test_blocked_row_excludes_raw_row_text(self) -> None:
        """The review blocked row dataclass must not have a raw_row_text
        field."""
        assert not hasattr(PdfStatementImportReviewBlockedRow, "raw_row_text")
