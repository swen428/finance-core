"""Tests for PDF Statement Review Queue Fixture v1.

Verifies that the review queue fixture correctly classifies parsed PDF
statement rows into ready_for_import, needs_review, and blocked; preserves
evidence fields; enforces review-only guard flags; and never touches a
database or live persistence.
"""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal

import pytest

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import (
    ParsedPdfStatementRow,
    PdfBlockedReason,
)
from finance_core.reconciliation.pdf_statement_evidence import (
    PdfDirectionConfidence,
    PdfDirectionSource,
    PdfOriginalAmountSign,
    PdfRowReviewStatus,
)
from finance_core.reconciliation.pdf_statement_review_queue_fixture import (
    PdfStatementReviewQueueFixture,
    PdfStatementReviewQueueRow,
    PdfStatementReviewQueueSummary,
    build_pdf_statement_review_queue,
    format_pdf_statement_review_queue_text,
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
    currency: str = "MYR",
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


# ===================================================================
# 1. All ready_for_import
# ===================================================================


class TestAllReadyForImport:
    """When all rows are valid bridge-passed rows with no warnings,
    every row must be classified as ready_for_import."""

    def test_all_ready_fixture(self) -> None:
        rows = tuple(
            _make_row(
                source_statement_id="stmt-a",
                description=f"Merchant {i}",
                amount=Decimal(str(i * 10)),
                transaction_date=date(2026, 7, i),
                source_row_ref=f"r{i}",
            )
            for i in range(1, 4)
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.summary.total_rows == 3
        assert fixture.summary.ready_for_import_count == 3
        assert fixture.summary.needs_review_count == 0
        assert fixture.summary.blocked_count == 0

    def test_all_ready_classification(self) -> None:
        rows = tuple(
            _make_row(
                description="Valid",
                transaction_date=date(2026, 7, 1),
                source_row_ref="r1",
            )
            for _ in range(3)
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        for r in fixture.rows:
            assert r.classification == "ready_for_import"

    def test_ready_rows_have_empty_blocked_reasons(self) -> None:
        rows = (
            _make_row(description="Valid", transaction_date=date(2026, 7, 1), source_row_ref="r1"),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].blocked_reasons == ()


# ===================================================================
# 2. Warning rows -> needs_review
# ===================================================================


class TestWarningRowsBecomeNeedsReview:
    """Rows with parse warnings but that pass bridge validation must be
    classified as needs_review."""

    def test_warning_row_is_needs_review(self) -> None:
        rows = (
            _make_row(
                description="Warning Row",
                transaction_date=date(2026, 7, 1),
                source_row_ref="w1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
            parse_statuses={"w1": "warning"},
        )
        assert fixture.summary.needs_review_count == 1
        assert fixture.summary.ready_for_import_count == 0
        assert fixture.summary.blocked_count == 0
        assert fixture.rows[0].classification == "needs_review"

    def test_warning_with_messages(self) -> None:
        rows = (
            _make_row(
                description="Warn Row",
                transaction_date=date(2026, 7, 1),
                source_row_ref="w2",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
            parse_statuses={"w2": "warning"},
            parser_warnings={"w2": ("low confidence", "date ambiguous")},
        )
        r = fixture.rows[0]
        assert r.classification == "needs_review"
        assert r.warnings == ("low confidence", "date ambiguous")
        assert fixture.summary.warnings_count == 2

    def test_warning_row_preserves_evidence(self) -> None:
        rows = (
            _make_row(
                description="Evidence Test",
                attachment_path="/tmp/evidence.pdf",
                amount=Decimal("55.50"),
                transaction_date=date(2026, 7, 1),
                source_row_ref="w3",
                raw_row_text="01/07 Evidence Test  55.50",
                source_page_number=2,
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/evidence.pdf",
            parse_statuses={"w3": "warning"},
        )
        r = fixture.rows[0]
        assert r.raw_row_text == "01/07 Evidence Test  55.50"
        assert r.source_page_number == 2
        assert r.attachment_path == "/tmp/evidence.pdf"


# ===================================================================
# 3. Parse error rows -> blocked
# ===================================================================


class TestParseErrorRowsBecomeBlocked:
    """Rows with parse_status == "error" must be classified as blocked,
    even if the bridge would not otherwise block them."""

    def test_parse_error_is_blocked(self) -> None:
        rows = (
            _make_row(
                description="Error Row",
                transaction_date=date(2026, 7, 1),
                source_row_ref="e1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
            parse_statuses={"e1": "error"},
        )
        assert fixture.rows[0].classification == "blocked"
        assert fixture.summary.blocked_count == 1

    def test_parse_error_not_in_ready(self) -> None:
        rows = (
            _make_row(
                description="Error",
                amount=Decimal("50.00"),
                transaction_date=date(2026, 7, 1),
                source_row_ref="e2",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
            parse_statuses={"e2": "error"},
        )
        assert fixture.summary.ready_for_import_count == 0
        assert fixture.summary.blocked_count == 1


# ===================================================================
# 4. Missing required fields -> blocked
# ===================================================================


class TestMissingFieldsBlocked:
    """Rows missing required fields (amount, date, currency, description)
    must be classified as blocked."""

    def test_missing_amount_blocked(self) -> None:
        rows = (
            _make_row(
                description="No Amount",
                amount=None,
                transaction_date=date(2026, 7, 1),
                source_row_ref="ma1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        r = fixture.rows[0]
        assert r.classification == "blocked"
        assert PdfBlockedReason.MISSING_AMOUNT in r.blocked_reasons

    def test_missing_date_blocked(self) -> None:
        rows = (
            _make_row(
                description="No Date",
                amount=Decimal("10.00"),
                transaction_date=None,
                posted_date=None,
                source_row_ref="md1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        r = fixture.rows[0]
        assert r.classification == "blocked"
        assert PdfBlockedReason.MISSING_USABLE_DATE in r.blocked_reasons

    def test_missing_currency_blocked(self) -> None:
        rows = (
            _make_row(
                description="No Currency",
                amount=Decimal("10.00"),
                currency="",
                transaction_date=date(2026, 7, 1),
                source_row_ref="mc1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        r = fixture.rows[0]
        assert r.classification == "blocked"
        assert PdfBlockedReason.MISSING_CURRENCY in r.blocked_reasons

    def test_missing_description_blocked(self) -> None:
        rows = (
            _make_row(
                description="",
                amount=Decimal("10.00"),
                transaction_date=date(2026, 7, 1),
                source_row_ref="de1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        r = fixture.rows[0]
        assert r.classification == "blocked"
        assert PdfBlockedReason.MISSING_DESCRIPTION in r.blocked_reasons


# ===================================================================
# 5. Unknown direction -> not ready_for_import
# ===================================================================


class TestUnknownDirection:
    """Rows with UNKNOWN amount direction must not be classified as
    ready_for_import."""

    def test_unknown_direction_blocked(self) -> None:
        rows = (
            _make_row(
                description="Unknown Dir",
                amount=Decimal("99.99"),
                transaction_date=date(2026, 7, 1),
                amount_direction=StatementAmountDirection.UNKNOWN,
                source_row_ref="ud1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        r = fixture.rows[0]
        assert r.classification == "blocked"
        assert PdfBlockedReason.UNKNOWN_DIRECTION in r.blocked_reasons

    def test_unknown_direction_not_ready(self) -> None:
        rows = (
            _make_row(
                description="Unknown Dir 2",
                amount=Decimal("50.00"),
                transaction_date=date(2026, 7, 1),
                amount_direction=StatementAmountDirection.UNKNOWN,
                source_row_ref="ud2",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.summary.ready_for_import_count == 0


# ===================================================================
# 6. Evidence preservation
# ===================================================================


class TestEvidencePreservation:
    """Evidence fields must be preserved in the review queue row output."""

    def test_attachment_path_preserved(self) -> None:
        rows = (
            _make_row(
                description="Path Test",
                attachment_path="/real/path/stmt.pdf",
                transaction_date=date(2026, 7, 1),
                source_row_ref="ep1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/real/path/stmt.pdf",
        )
        assert fixture.rows[0].attachment_path == "/real/path/stmt.pdf"
        assert fixture.summary.attachment_path == "/real/path/stmt.pdf"

    def test_source_statement_id_preserved(self) -> None:
        rows = (
            _make_row(
                description="ID Test",
                source_statement_id="pdf-tmpl-cli-abc123",
                transaction_date=date(2026, 7, 1),
                source_row_ref="ep2",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="pdf-tmpl-cli-abc123",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].source_statement_id == "pdf-tmpl-cli-abc123"

    def test_source_row_ref_preserved(self) -> None:
        rows = (
            _make_row(
                description="Ref Test",
                source_row_ref="row-5",
                transaction_date=date(2026, 7, 1),
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].source_row_ref == "row-5"

    def test_raw_row_text_preserved(self) -> None:
        raw = "01/07 Test Merchant  100.00"
        rows = (
            _make_row(
                description="Test Merchant",
                transaction_date=date(2026, 7, 1),
                raw_row_text=raw,
                source_row_ref="ep3",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].raw_row_text == raw

    def test_transaction_date_preserved(self) -> None:
        d = date(2026, 7, 15)
        rows = (
            _make_row(
                description="Date Test",
                transaction_date=d,
                source_row_ref="ep4",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].transaction_date == d

    def test_posted_date_preserved(self) -> None:
        d = date(2026, 7, 16)
        rows = (
            _make_row(
                description="Posted Test",
                transaction_date=date(2026, 7, 15),
                posted_date=d,
                source_row_ref="ep5",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].posted_date == d

    def test_amount_preserved(self) -> None:
        rows = (
            _make_row(
                description="Amount Test",
                amount=Decimal("123.45"),
                transaction_date=date(2026, 7, 1),
                source_row_ref="ep6",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].amount == Decimal("123.45")

    def test_currency_preserved(self) -> None:
        rows = (
            _make_row(
                description="Currency Test",
                currency="SGD",
                transaction_date=date(2026, 7, 1),
                source_row_ref="ep7",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].currency == "SGD"

    def test_amount_direction_preserved(self) -> None:
        rows = (
            _make_row(
                description="Dir Test",
                amount_direction=StatementAmountDirection.REFUND,
                transaction_date=date(2026, 7, 1),
                source_row_ref="ep8",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].amount_direction == StatementAmountDirection.REFUND

    def test_source_page_number_preserved(self) -> None:
        rows = (
            _make_row(
                description="Page Test",
                source_page_number=3,
                transaction_date=date(2026, 7, 1),
                source_row_ref="ep9",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].source_page_number == 3

    def test_no_amount_preserved(self) -> None:
        rows = (
            _make_row(
                description="None Amount",
                amount=None,
                transaction_date=date(2026, 7, 1),
                source_row_ref="ep10",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].amount is None

    def test_none_posted_date_preserved(self) -> None:
        rows = (
            _make_row(
                description="No Posted",
                transaction_date=date(2026, 7, 1),
                posted_date=None,
                source_row_ref="ep11",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].posted_date is None


# ===================================================================
# 7. review_only and not_final_financial_record always True
# ===================================================================


class TestReviewOnlyGuards:
    """review_only and not_final_financial_record must be True on every
    row and on the summary."""

    def test_row_guards_ready(self) -> None:
        rows = (
            _make_row(
                description="Guard Test",
                transaction_date=date(2026, 7, 1),
                source_row_ref="g1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        r = fixture.rows[0]
        assert r.review_only is True
        assert r.not_final_financial_record is True

    def test_row_guards_needs_review(self) -> None:
        rows = (
            _make_row(
                description="Guard Warn",
                transaction_date=date(2026, 7, 1),
                source_row_ref="g2",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
            parse_statuses={"g2": "warning"},
        )
        r = fixture.rows[0]
        assert r.review_only is True
        assert r.not_final_financial_record is True

    def test_row_guards_blocked(self) -> None:
        rows = (
            _make_row(
                description="Guard Block",
                amount=None,
                transaction_date=date(2026, 7, 1),
                source_row_ref="g3",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        r = fixture.rows[0]
        assert r.review_only is True
        assert r.not_final_financial_record is True

    def test_summary_guards(self) -> None:
        rows = (
            _make_row(
                description="Guard Summary",
                transaction_date=date(2026, 7, 1),
                source_row_ref="g4",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        s = fixture.summary
        assert s.review_only is True
        assert s.not_final_financial_record is True


# ===================================================================
# 8. Aggregate counts
# ===================================================================


class TestAggregateCounts:
    """Aggregate summary counts must be correct for mixed row sets."""

    def test_mixed_counts(self) -> None:
        rows = (
            # ready
            _make_row(description="R1", transaction_date=date(2026, 7, 1), source_row_ref="r1"),
            _make_row(description="R2", transaction_date=date(2026, 7, 2), source_row_ref="r2"),
            # needs_review (warning)
            _make_row(description="NR1", transaction_date=date(2026, 7, 3), source_row_ref="nr1"),
            # blocked
            _make_row(
                description="B1",
                amount=None,
                transaction_date=date(2026, 7, 4),
                source_row_ref="b1",
            ),
            _make_row(
                description="B2",
                amount=None,
                transaction_date=date(2026, 7, 5),
                source_row_ref="b2",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-mix",
            attachment_path="/tmp/fake.pdf",
            parse_statuses={"nr1": "warning"},
        )
        s = fixture.summary
        assert s.total_rows == 5
        assert s.ready_for_import_count == 2
        assert s.needs_review_count == 1
        assert s.blocked_count == 2
        assert s.total_rows == s.ready_for_import_count + s.needs_review_count + s.blocked_count

    def test_total_counts_always_consistent(self) -> None:
        """The three classification counts must always sum to total_rows."""
        rows = (
            _make_row(description="R", transaction_date=date(2026, 7, 1), source_row_ref="a"),
            _make_row(description="N", transaction_date=date(2026, 7, 2), source_row_ref="b"),
            _make_row(
                description="B", amount=None, transaction_date=date(2026, 7, 3), source_row_ref="c"
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
            parse_statuses={"b": "warning"},
        )
        s = fixture.summary
        assert s.total_rows == 3
        assert s.ready_for_import_count + s.needs_review_count + s.blocked_count == s.total_rows

    def test_empty_input_all_zero(self) -> None:
        fixture = build_pdf_statement_review_queue(
            (),
            source_statement_id="stmt-empty",
            attachment_path="/tmp/fake.pdf",
        )
        s = fixture.summary
        assert s.total_rows == 0
        assert s.ready_for_import_count == 0
        assert s.needs_review_count == 0
        assert s.blocked_count == 0
        assert s.warnings_count == 0
        assert s.blocked_reason_counts == ()
        assert fixture.rows == ()

    def test_source_statement_id_in_summary(self) -> None:
        rows = (
            _make_row(description="SID", transaction_date=date(2026, 7, 1), source_row_ref="s1"),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="pdf-tmpl-cli-deadbeef",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.summary.source_statement_id == "pdf-tmpl-cli-deadbeef"

    def test_template_id_in_summary(self) -> None:
        rows = (
            _make_row(description="TID", transaction_date=date(2026, 7, 1), source_row_ref="t1"),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
            template_id="sample_bank_v1",
        )
        assert fixture.summary.template_id == "sample_bank_v1"

    def test_template_id_none_by_default(self) -> None:
        rows = (
            _make_row(
                description="TID Def", transaction_date=date(2026, 7, 1), source_row_ref="t2"
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.summary.template_id is None


# ===================================================================
# 9. Deterministic source grouping
# ===================================================================


class TestDeterministicSourceGrouping:
    """Multiple rows sharing the same source_statement_id must all carry
    that identifier, and ordering must be deterministic."""

    def test_same_source_statement_id_on_all_rows(self) -> None:
        sid = "pdf-tmpl-cli-abc123456789"
        rows = tuple(
            _make_row(
                source_statement_id=sid,
                description=f"Row {i}",
                transaction_date=date(2026, 7, i),
                source_row_ref=f"r{i}",
            )
            for i in range(1, 4)
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id=sid,
            attachment_path="/tmp/fake.pdf",
        )
        for r in fixture.rows:
            assert r.source_statement_id == sid
        assert fixture.summary.source_statement_id == sid

    def test_ordering_preserved(self) -> None:
        rows = tuple(
            _make_row(
                description=f"Item {i}",
                transaction_date=date(2026, 7, i),
                source_row_ref=f"o{i}",
            )
            for i in range(1, 6)
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        for i, r in enumerate(fixture.rows):
            assert r.description == f"Item {i + 1}"

    def test_deterministic_identical_calls(self) -> None:
        rows = tuple(
            _make_row(
                description=f"D{i}",
                transaction_date=date(2026, 7, i),
                source_row_ref=f"d{i}",
            )
            for i in range(1, 4)
        )
        f1 = build_pdf_statement_review_queue(
            rows, source_statement_id="stmt-a", attachment_path="/tmp/fake.pdf"
        )
        f2 = build_pdf_statement_review_queue(
            rows, source_statement_id="stmt-a", attachment_path="/tmp/fake.pdf"
        )
        assert f1.summary.total_rows == f2.summary.total_rows
        assert f1.summary.ready_for_import_count == f2.summary.ready_for_import_count
        for a, b in zip(f1.rows, f2.rows):
            assert a.classification == b.classification
            assert a.description == b.description


# ===================================================================
# 10. Text formatter
# ===================================================================


class TestTextFormatter:
    """The text formatter must include counts, blocking reasons, and
    statement identity."""

    def test_formatter_includes_counts(self) -> None:
        rows = tuple(
            _make_row(
                description=f"R{i}", transaction_date=date(2026, 7, i), source_row_ref=f"r{i}"
            )
            for i in range(1, 4)
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-fmt",
            attachment_path="/tmp/fmt.pdf",
        )
        text = format_pdf_statement_review_queue_text(fixture)
        assert "Total rows:" in text
        assert "Ready for import:" in text
        assert "Needs review:" in text
        assert "Blocked:" in text
        assert "Warnings:" in text
        assert "3" in text  # total rows

    def test_formatter_includes_source_statement_id(self) -> None:
        rows = (
            _make_row(description="FMT", transaction_date=date(2026, 7, 1), source_row_ref="f1"),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="pdf-tmpl-cli-src",
            attachment_path="/tmp/fmt.pdf",
        )
        text = format_pdf_statement_review_queue_text(fixture)
        assert "pdf-tmpl-cli-src" in text

    def test_formatter_includes_attachment_path(self) -> None:
        rows = (
            _make_row(description="FMT", transaction_date=date(2026, 7, 1), source_row_ref="f2"),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-fmt",
            attachment_path="/real/stmt.pdf",
        )
        text = format_pdf_statement_review_queue_text(fixture)
        assert "/real/stmt.pdf" in text

    def test_formatter_includes_template_id_when_present(self) -> None:
        rows = (
            _make_row(description="FMT", transaction_date=date(2026, 7, 1), source_row_ref="f3"),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-fmt",
            attachment_path="/tmp/fmt.pdf",
            template_id="sample_bank_v1",
        )
        text = format_pdf_statement_review_queue_text(fixture)
        assert "sample_bank_v1" in text

    def test_formatter_includes_blocked_reasons(self) -> None:
        rows = (
            _make_row(
                description="Blocked Merch",
                amount=None,
                transaction_date=date(2026, 7, 1),
                source_row_ref="bf1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-fmt",
            attachment_path="/tmp/fmt.pdf",
        )
        text = format_pdf_statement_review_queue_text(fixture)
        assert "missing_amount" in text
        assert "Blocked reason breakdown:" in text or "BLOCKED ROWS" in text

    def test_formatter_includes_blocked_section(self) -> None:
        rows = (
            _make_row(
                description="Blocked",
                amount=None,
                transaction_date=date(2026, 7, 1),
                source_row_ref="bf2",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-fmt",
            attachment_path="/tmp/fmt.pdf",
        )
        text = format_pdf_statement_review_queue_text(fixture)
        assert "BLOCKED ROWS" in text

    def test_formatter_includes_needs_review_section(self) -> None:
        rows = (
            _make_row(
                description="Review Me", transaction_date=date(2026, 7, 1), source_row_ref="nr1"
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-fmt",
            attachment_path="/tmp/fmt.pdf",
            parse_statuses={"nr1": "warning"},
            parser_warnings={"nr1": ("low confidence",)},
        )
        text = format_pdf_statement_review_queue_text(fixture)
        assert "NEEDS REVIEW" in text
        assert "low confidence" in text

    def test_formatter_ready_section(self) -> None:
        rows = (
            _make_row(description="Ready", transaction_date=date(2026, 7, 1), source_row_ref="rd1"),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-fmt",
            attachment_path="/tmp/fmt.pdf",
        )
        text = format_pdf_statement_review_queue_text(fixture)
        assert "READY FOR IMPORT" in text
        assert "1 row(s)" in text

    def test_formatter_includes_review_only_guard(self) -> None:
        rows = (
            _make_row(description="G", transaction_date=date(2026, 7, 1), source_row_ref="rr1"),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-fmt",
            attachment_path="/tmp/fmt.pdf",
        )
        text = format_pdf_statement_review_queue_text(fixture)
        assert "review_only=True" in text
        assert "not_final_financial_record=True" in text

    def test_formatter_empty_blocked(self) -> None:
        rows = (
            _make_row(description="OK", transaction_date=date(2026, 7, 1), source_row_ref="e1"),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-fmt",
            attachment_path="/tmp/fmt.pdf",
        )
        text = format_pdf_statement_review_queue_text(fixture)
        assert "BLOCKED ROWS" not in text  # no blocked rows
        assert "Blocked reason breakdown:" not in text  # no blocked reasons

    def test_formatter_empty_needs_review(self) -> None:
        rows = (
            _make_row(description="OK", transaction_date=date(2026, 7, 1), source_row_ref="e2"),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-fmt",
            attachment_path="/tmp/fmt.pdf",
        )
        text = format_pdf_statement_review_queue_text(fixture)
        assert "NEEDS REVIEW" not in text  # no needs_review rows

    def test_formatter_deterministic(self) -> None:
        rows = tuple(
            _make_row(
                description=f"D{i}", transaction_date=date(2026, 7, i), source_row_ref=f"d{i}"
            )
            for i in range(1, 4)
        )
        fixture = build_pdf_statement_review_queue(
            rows, source_statement_id="stmt-fmt", attachment_path="/tmp/fmt.pdf"
        )
        t1 = format_pdf_statement_review_queue_text(fixture)
        t2 = format_pdf_statement_review_queue_text(fixture)
        assert t1 == t2

    def test_formatter_blocked_row_includes_description(self) -> None:
        rows = (
            _make_row(
                description="BLOCKED DESC",
                amount=None,
                transaction_date=date(2026, 7, 1),
                source_row_ref="bd1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-fmt",
            attachment_path="/tmp/fmt.pdf",
        )
        text = format_pdf_statement_review_queue_text(fixture)
        assert "BLOCKED DESC" in text


# ===================================================================
# 11. No live DB access / no persistence
# ===================================================================


class TestNoLiveDatabaseReference:
    """Module must not reference database/finance.db, sqlite3, or file I/O."""

    def test_no_live_db_path(self) -> None:
        from finance_core.reconciliation import pdf_statement_review_queue_fixture as mod

        source = inspect.getsource(mod)
        assert "finance.db" not in source

    def test_no_sqlite3_import(self) -> None:
        from finance_core.reconciliation import pdf_statement_review_queue_fixture as mod

        source = inspect.getsource(mod)
        assert "sqlite3" not in source

    def test_no_pdf_ocr_dependencies(self) -> None:
        from finance_core.reconciliation import pdf_statement_review_queue_fixture as mod

        source = inspect.getsource(mod)
        source_lower = source.lower()
        for dep in ("pdfplumber", "pytesseract", "pypdf", "camelot", "tabula"):
            assert dep not in source_lower, f"Unexpected dependency: {dep}"

    def test_no_file_io(self) -> None:
        from finance_core.reconciliation import pdf_statement_review_queue_fixture as mod

        source = inspect.getsource(mod)
        assert "open(" not in source

    def test_no_persistence_imports(self) -> None:
        from finance_core.reconciliation import pdf_statement_review_queue_fixture as mod

        source = inspect.getsource(mod)
        # Check for actual persistence imports (not docstring mentions of non-goals)
        assert "from .persistence" not in source
        assert "from finance_core.reconciliation.persistence" not in source

    def test_no_import_persistence(self) -> None:
        from finance_core.reconciliation import pdf_statement_review_queue_fixture as mod

        source = inspect.getsource(mod)
        assert "from .persistence" not in source
        assert "from finance_core.reconciliation.persistence" not in source

    def test_no_statement_import(self) -> None:
        from finance_core.reconciliation import pdf_statement_review_queue_fixture as mod

        source = inspect.getsource(mod)
        assert "statement_import" not in source.lower()

    def test_no_apply_import(self) -> None:
        from finance_core.reconciliation import pdf_statement_review_queue_fixture as mod

        source = inspect.getsource(mod)
        assert "from .apply" not in source
        assert "finance_core.reconciliation.apply" not in source


# ===================================================================
# 12. ParsedPdfStatementRow compatibility
# ===================================================================


class TestParsedPdfStatementRowCompatibility:
    """The fixture must accept ParsedPdfStatementRow objects as produced
    by the template adapter and the bridge contract."""

    def test_legacy_adapter_shape_without_evidence_is_blocked(self) -> None:
        row = ParsedPdfStatementRow(
            source_statement_id="pdf-tmpl-cli-abc",
            attachment_path="/tmp/sample.pdf",
            description="Coffee",
            amount=Decimal("5.50"),
            currency="MYR",
            source_page_number=1,
            source_row_ref="row-1",
            transaction_date=date(2026, 7, 1),
            posted_date=date(2026, 7, 2),
            raw_row_text="01/07 Coffee  5.50",
            amount_direction=StatementAmountDirection.DEBIT,
        )
        fixture = build_pdf_statement_review_queue(
            (row,),
            source_statement_id="pdf-tmpl-cli-abc",
            attachment_path="/tmp/sample.pdf",
        )
        assert fixture.summary.total_rows == 1
        assert fixture.rows[0].classification == "blocked"

    def test_accepts_rows_with_multiple_amount_directions(self) -> None:
        directions = [
            StatementAmountDirection.DEBIT,
            StatementAmountDirection.CREDIT,
            StatementAmountDirection.REFUND,
            StatementAmountDirection.PAYMENT,
            StatementAmountDirection.FEE,
            StatementAmountDirection.INTEREST,
        ]
        rows = tuple(
            _make_row(
                description=f"Dir {d.value}",
                amount=Decimal("10.00"),
                transaction_date=date(2026, 7, 1),
                amount_direction=d,
                source_row_ref=f"d_{d.value}",
            )
            for d in directions
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-dir",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.summary.ready_for_import_count == len(directions) - 1
        assert fixture.summary.blocked_count == 1

    def test_row_with_all_optionals_none_is_blocked(self) -> None:
        row = ParsedPdfStatementRow(
            source_statement_id="stmt-min",
            attachment_path="/tmp/min.pdf",
            description="Minimal",
            amount=Decimal("1.00"),
            currency="MYR",
            transaction_date=date(2026, 7, 1),
            amount_direction=StatementAmountDirection.DEBIT,
        )
        fixture = build_pdf_statement_review_queue(
            (row,),
            source_statement_id="stmt-min",
            attachment_path="/tmp/min.pdf",
        )
        r = fixture.rows[0]
        assert r.source_page_number is None
        assert r.source_row_ref is None
        assert r.posted_date is None
        assert r.classification == "blocked"

    def test_row_with_bridge_defaults_is_blocked(self) -> None:
        row = ParsedPdfStatementRow(
            source_statement_id="stmt-bridge",
            attachment_path="/tmp/bridge.pdf",
            description="Bridge Test",
            amount=Decimal("50.00"),
            currency="SGD",
            transaction_date=date(2026, 7, 5),
            amount_direction=StatementAmountDirection.DEBIT,
        )
        fixture = build_pdf_statement_review_queue(
            (row,),
            source_statement_id="stmt-bridge",
            attachment_path="/tmp/bridge.pdf",
        )
        r = fixture.rows[0]
        assert r.classification == "blocked"
        assert r.amount_direction == StatementAmountDirection.DEBIT


# ===================================================================
# 13. Immutability
# ===================================================================


class TestImmutability:
    """All exported dataclasses must be frozen."""

    def test_queue_row_is_frozen(self) -> None:
        rows = (
            _make_row(
                description="Immutable", transaction_date=date(2026, 7, 1), source_row_ref="im1"
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        r = fixture.rows[0]
        with pytest.raises(Exception):
            r.description = "changed"  # type: ignore[misc]

    def test_summary_is_frozen(self) -> None:
        rows = (
            _make_row(
                description="Immutable", transaction_date=date(2026, 7, 1), source_row_ref="im2"
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        with pytest.raises(Exception):
            fixture.summary.total_rows = 99  # type: ignore[misc]

    def test_fixture_is_frozen(self) -> None:
        rows = (
            _make_row(
                description="Immutable", transaction_date=date(2026, 7, 1), source_row_ref="im3"
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        with pytest.raises(Exception):
            fixture.rows = ()  # type: ignore[misc]


# ===================================================================
# 14. Conservative classification edge cases
# ===================================================================


class TestConservativeClassification:
    """Classification must err on the side of caution: ambiguous rows
    should be needs_review or blocked, not ready_for_import."""

    def test_warnings_without_explicit_parse_status_becomes_needs_review(self) -> None:
        """Rows with warning messages but no parse_status should be
        needs_review if bridge passes."""
        rows = (
            _make_row(
                description="Has warnings",
                transaction_date=date(2026, 7, 1),
                source_row_ref="cw1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
            parser_warnings={"cw1": ("date format non-standard",)},
        )
        r = fixture.rows[0]
        assert r.classification == "needs_review"
        assert r.parse_status is None  # not set, but still classified

    def test_multiple_reasons_all_present(self) -> None:
        """Row with multiple blocked reasons must have all of them."""
        rows = (
            _make_row(
                description="",
                amount=None,
                currency="",
                transaction_date=None,
                posted_date=None,
                amount_direction=StatementAmountDirection.UNKNOWN,
                source_row_ref="mr1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        r = fixture.rows[0]
        assert r.classification == "blocked"
        reason_values = {rv.value for rv in r.blocked_reasons}
        assert "missing_amount" in reason_values
        assert "unknown_direction" in reason_values
        assert "missing_currency" in reason_values
        assert "missing_usable_date" in reason_values
        assert "missing_description" in reason_values

    def test_blocked_rows_still_preserve_evidence(self) -> None:
        """Even blocked rows must preserve evidence fields."""
        rows = (
            _make_row(
                description="Block Evid",
                amount=None,
                transaction_date=date(2026, 7, 1),
                raw_row_text="raw-blocked",
                source_page_number=5,
                source_row_ref="be1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/evidence.pdf",
        )
        r = fixture.rows[0]
        assert r.raw_row_text == "raw-blocked"
        assert r.source_page_number == 5
        assert r.description == "Block Evid"

    def test_blocked_reason_counts_deterministic(self) -> None:
        rows = tuple(
            _make_row(
                description=f"B{i}",
                amount=None,
                transaction_date=date(2026, 7, i),
                source_row_ref=f"bc{i}",
            )
            for i in range(1, 4)
        )
        f1 = build_pdf_statement_review_queue(
            rows, source_statement_id="stmt-a", attachment_path="/tmp/fake.pdf"
        )
        f2 = build_pdf_statement_review_queue(
            rows, source_statement_id="stmt-a", attachment_path="/tmp/fake.pdf"
        )
        assert f1.summary.blocked_reason_counts == f2.summary.blocked_reason_counts

    def test_one_date_is_enough_for_ready(self) -> None:
        """transaction_date alone is sufficient for bridge to pass;
        posted_date is optional."""
        rows = (
            _make_row(
                description="Txn Only",
                transaction_date=date(2026, 7, 1),
                posted_date=None,
                source_row_ref="od1",
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].classification == "ready_for_import"

    def test_parse_status_defaults_to_none(self) -> None:
        rows = (
            _make_row(
                description="No status", transaction_date=date(2026, 7, 1), source_row_ref="ps1"
            ),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert fixture.rows[0].parse_status is None


# ===================================================================
# 15. Public API shape
# ===================================================================


class TestPublicAPI:
    """Verify the module exposes the expected public API."""

    def test_all_exports(self) -> None:
        from finance_core.reconciliation import pdf_statement_review_queue_fixture as mod

        assert hasattr(mod, "__all__")
        expected = {
            "PdfStatementReviewQueueRow",
            "PdfStatementReviewQueueSummary",
            "PdfStatementReviewQueueFixture",
            "build_pdf_statement_review_queue",
            "format_pdf_statement_review_queue_text",
        }
        assert set(mod.__all__) == expected

    def test_fixture_type(self) -> None:
        rows = (
            _make_row(description="Type", transaction_date=date(2026, 7, 1), source_row_ref="ty1"),
        )
        fixture = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-a",
            attachment_path="/tmp/fake.pdf",
        )
        assert isinstance(fixture, PdfStatementReviewQueueFixture)
        assert isinstance(fixture.summary, PdfStatementReviewQueueSummary)
        assert isinstance(fixture.rows[0], PdfStatementReviewQueueRow)
