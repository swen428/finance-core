"""Tests for PDF Statement Import Run Review Summary v1.

Verifies that the run review summary correctly classifies run status,
preserves evidence fields, enforces review-only guard flags, produces
deterministic output, separates dashboard-safe from audit payloads,
and never touches database/finance.db, seed data, migrations, final
financial records, or settlement obligations.
"""

from __future__ import annotations

import inspect
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import (
    ParsedPdfStatementRow,
)
from finance_core.reconciliation.pdf_statement_evidence import (
    PdfDirectionConfidence,
    PdfDirectionSource,
    PdfOriginalAmountSign,
    PdfRowReviewStatus,
)
from finance_core.reconciliation.pdf_statement_import_run_review_summary import (
    PdfStatementImportRunReviewSummary,
    RunReviewStatus,
    _build_warning_reason_counts,
    _classify_run_status,
    build_import_run_review_summary_from_bridge_result,
    build_import_run_review_summary_from_import_result,
    export_run_review_summary_audit_payload,
    export_run_review_summary_dashboard_payload,
    format_import_run_review_summary_text,
)
from finance_core.reconciliation.pdf_statement_review_queue_bridge import (
    run_pdf_statement_review_queue_bridge,
)
from finance_core.reconciliation.pdf_statement_review_queue_fixture import (
    PdfStatementReviewQueueFixture,
    build_pdf_statement_review_queue,
)
from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
    DEFAULT_PDF_FIXTURE_PATH,
    DEFAULT_TEMPLATE_ID,
    DEFAULT_TEXT_FIXTURE_PATH,
    PdfStatementTempDbImportResult,
    import_pdf_statement_fixture_to_temp_db,
)
from finance_core.reconciliation.statement_import_contracts import StatementImportBatch


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


def _build_blocked_review_queue() -> PdfStatementReviewQueueFixture:
    rows = (
        _make_row(
            description="",
            amount=None,
            currency="",
            transaction_date=None,
            posted_date=None,
            amount_direction=StatementAmountDirection.UNKNOWN,
            source_row_ref="br1",
        ),
        _make_row(
            description="Bad Row",
            amount=None,
            transaction_date=date(2026, 7, 1),
            source_row_ref="br2",
        ),
    )
    return build_pdf_statement_review_queue(
        rows,
        source_statement_id="stmt-blocked-test",
        attachment_path="/tmp/blocked.pdf",
    )


def _build_minimal_import_result(
    rq: PdfStatementReviewQueueFixture,
    batch_public_id: str,
    pdf_parsing_mode: str = "fixture_text",
) -> PdfStatementTempDbImportResult:
    import_batch = StatementImportBatch(
        batch_id=1,
        public_id=batch_public_id,
        source_type="bank_statement",
        row_count=rq.summary.total_rows,
        inserted_ids=[],
        skipped_duplicates=0,
        idempotent_count=0,
    )
    return PdfStatementTempDbImportResult(
        db_path="/tmp/test.db",
        pdf_path=rq.summary.attachment_path,
        text_fixture_path="/tmp/test.txt",
        parse_result=None,  # type: ignore[arg-type]
        adapter_result=None,  # type: ignore[arg-type]
        review_queue=rq,
        normalization_result=None,  # type: ignore[arg-type]
        import_batch=import_batch,  # type: ignore[arg-type]
        source_type="bank_statement",
        pdf_parsing_mode=pdf_parsing_mode,
    )


def _make_summary(
    *,
    run_reference: str = "run-test",
    batch_public_id: str = "batch-test",
    total_row_count: int = 3,
    ready_for_import_count: int = 3,
    needs_review_count: int = 0,
    blocked_count: int = 0,
    run_status: RunReviewStatus = "fully_ready",
) -> PdfStatementImportRunReviewSummary:
    return PdfStatementImportRunReviewSummary(
        run_reference=run_reference,
        batch_public_id=batch_public_id,
        source_pdf_path="/tmp/test.pdf",
        source_statement_id="stmt-test",
        template_id="tpl-test",
        source_mode="fixture_text",
        pdf_parsing_mode="fixture_text",
        total_row_count=total_row_count,
        ready_for_import_count=ready_for_import_count,
        needs_review_count=needs_review_count,
        blocked_count=blocked_count,
        blocked_reason_counts=(),
        warning_reason_counts=(),
        run_status=run_status,
        evidence_source_refs=("/tmp/test.pdf", "stmt-test", "template:tpl-test"),
    )


def _make_full_summary(
    run_status: RunReviewStatus = "fully_ready",
) -> PdfStatementImportRunReviewSummary:
    return PdfStatementImportRunReviewSummary(
        run_reference="run-full",
        batch_public_id="batch-full",
        source_pdf_path="/full/path.pdf",
        source_statement_id="stmt-full",
        template_id="sample_bank_v1",
        source_mode="fixture_text",
        pdf_parsing_mode="fixture_text",
        total_row_count=3,
        ready_for_import_count=2,
        needs_review_count=1,
        blocked_count=0,
        blocked_reason_counts=(),
        warning_reason_counts=(("low confidence", 1),),
        run_status=run_status,
        evidence_source_refs=("/full/path.pdf", "stmt-full", "template:sample_bank_v1"),
    )


# ===================================================================
# Run status classification
# ===================================================================


class TestRunStatusClassification:
    def test_all_ready_fully_ready(self) -> None:
        assert _classify_run_status(5, 0, 0) == "fully_ready"

    def test_mixed_ready_and_review_partially_reviewable(self) -> None:
        assert _classify_run_status(3, 2, 0) == "partially_reviewable"

    def test_blocked_rows_blocked(self) -> None:
        assert _classify_run_status(3, 0, 1) == "blocked"

    def test_blocked_with_review_blocked(self) -> None:
        assert _classify_run_status(1, 2, 1) == "blocked"

    def test_empty_run_fully_ready(self) -> None:
        assert _classify_run_status(0, 0, 0) == "fully_ready"

    def test_only_needs_review_partially_reviewable(self) -> None:
        assert _classify_run_status(0, 3, 0) == "partially_reviewable"


# ===================================================================
# Warning reason counts
# ===================================================================


class TestWarningReasonCounts:
    def test_empty_warnings(self) -> None:
        assert _build_warning_reason_counts(()) == ()

    def test_single_warning(self) -> None:
        result = _build_warning_reason_counts(("low confidence",))
        assert result == (("low confidence", 1),)

    def test_multiple_warnings_sorted(self) -> None:
        result = _build_warning_reason_counts(
            ("date ambiguous", "low confidence", "date ambiguous")
        )
        assert result == (("date ambiguous", 2), ("low confidence", 1))

    def test_deterministic(self) -> None:
        warnings = ("a", "b", "a", "c", "b", "a")
        r1 = _build_warning_reason_counts(warnings)
        r2 = _build_warning_reason_counts(warnings)
        assert r1 == r2

    def test_empty_strings_filtered(self) -> None:
        result = _build_warning_reason_counts(("", "real", ""))
        assert result == (("real", 1),)

    def test_all_empty(self) -> None:
        result = _build_warning_reason_counts(("", "", ""))
        assert result == ()


# ===================================================================
# Direct construction and immutability
# ===================================================================


class TestDirectConstruction:
    def test_can_construct(self) -> None:
        s = _make_summary()
        assert s.run_status == "fully_ready"
        assert s.review_only is True
        assert s.not_final_financial_record is True

    def test_immutable(self) -> None:
        s = _make_summary(run_status="fully_ready")
        with pytest.raises(Exception):
            s.run_status = "blocked"  # type: ignore[misc]
        with pytest.raises(Exception):
            s.total_row_count = 99  # type: ignore[misc]

    def test_guard_flags_always_true(self) -> None:
        s = _make_summary()
        assert s.review_only is True
        assert s.not_final_financial_record is True


# ===================================================================
# All rows ready -> fully_ready (integration)
# ===================================================================


class TestAllRowsReadyIntegration:
    def test_all_ready_run_status(self) -> None:
        rows = tuple(
            _make_row(
                description=f"Merchant {i}",
                amount=Decimal(str(i * 10)),
                transaction_date=date(2026, 7, i),
                source_row_ref=f"r{i}",
            )
            for i in range(1, 4)
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-all-ready",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-all-ready")
        summary = build_import_run_review_summary_from_import_result(import_result)
        assert summary.run_status == "fully_ready"
        assert summary.total_row_count == 3
        assert summary.ready_for_import_count == 3
        assert summary.needs_review_count == 0
        assert summary.blocked_count == 0


# ===================================================================
# Mixed ready + needs_review -> partially_reviewable
# ===================================================================


class TestMixedReadyAndReview:
    def test_partially_reviewable(self) -> None:
        rows = (
            _make_row(
                description="Ready Row",
                transaction_date=date(2026, 7, 1),
                source_row_ref="rr1",
            ),
            _make_row(
                description="Review Row",
                transaction_date=date(2026, 7, 2),
                source_row_ref="rr2",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-mixed",
            attachment_path="/tmp/fake.pdf",
            parse_statuses={"rr2": "warning"},
            parser_warnings={"rr2": ("low confidence",)},
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-mixed")
        summary = build_import_run_review_summary_from_import_result(import_result)
        assert summary.run_status == "partially_reviewable"
        assert summary.ready_for_import_count == 1
        assert summary.needs_review_count == 1
        assert summary.blocked_count == 0


# ===================================================================
# Blocked rows -> blocked
# ===================================================================


class TestBlockedRowsStatus:
    def test_blocked_status(self) -> None:
        rows = (
            _make_row(
                description="Ready Row",
                transaction_date=date(2026, 7, 1),
                source_row_ref="r1",
            ),
            _make_row(
                description="",
                amount=None,
                transaction_date=date(2026, 7, 2),
                source_row_ref="b1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-blocked",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-blocked")
        summary = build_import_run_review_summary_from_import_result(import_result)
        assert summary.run_status == "blocked"
        assert summary.blocked_count == 1

    def test_all_blocked(self) -> None:
        rows = tuple(
            _make_row(
                description="",
                amount=None,
                transaction_date=date(2026, 7, i),
                source_row_ref=f"b{i}",
            )
            for i in range(1, 4)
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-all-blocked",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-all-blocked")
        summary = build_import_run_review_summary_from_import_result(import_result)
        assert summary.run_status == "blocked"
        assert summary.ready_for_import_count == 0
        assert summary.blocked_count == 3


# ===================================================================
# Blocked reason counts deterministic
# ===================================================================


class TestBlockedReasonCounts:
    def test_blocked_reason_counts_present(self) -> None:
        rows = (
            _make_row(
                description="Bad Row",
                amount=None,
                currency="",
                transaction_date=None,
                posted_date=None,
                source_row_ref="br1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-reason",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-reason")
        summary = build_import_run_review_summary_from_import_result(import_result)
        assert len(summary.blocked_reason_counts) > 0
        valid_reasons = {
            "missing_amount",
            "missing_currency",
            "missing_usable_date",
            "missing_description",
            "ambiguous_direction",
            "unsupported_layout",
            "unknown_direction",
            "missing_amount_token",
        }
        for reason_str, count in summary.blocked_reason_counts:
            assert reason_str in valid_reasons
            assert count > 0

    def test_blocked_reason_counts_deterministic(self) -> None:
        rq = _build_blocked_review_queue()
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-det")
        s1 = build_import_run_review_summary_from_import_result(import_result)
        s2 = build_import_run_review_summary_from_import_result(import_result)
        assert s1.blocked_reason_counts == s2.blocked_reason_counts

    def test_blocked_reason_counts_empty_when_no_blocked(self) -> None:
        rows = (
            _make_row(
                description="Clean",
                transaction_date=date(2026, 7, 1),
                source_row_ref="c1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-clean",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-clean")
        summary = build_import_run_review_summary_from_import_result(import_result)
        assert summary.blocked_reason_counts == ()


# ===================================================================
# Warning reason counts deterministic
# ===================================================================


class TestWarningReasonCountsInSummary:
    def test_warning_reason_counts_present(self) -> None:
        rows = (
            _make_row(
                description="Warn Row",
                transaction_date=date(2026, 7, 1),
                source_row_ref="w1",
            ),
            _make_row(
                description="Warn Row 2",
                transaction_date=date(2026, 7, 2),
                source_row_ref="w2",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-warn",
            attachment_path="/tmp/fake.pdf",
            parse_statuses={"w1": "warning", "w2": "warning"},
            parser_warnings={
                "w1": ("low confidence",),
                "w2": ("low confidence", "date ambiguous"),
            },
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-warn")
        summary = build_import_run_review_summary_from_import_result(import_result)
        assert len(summary.warning_reason_counts) > 0

    def test_warning_reason_counts_deterministic(self) -> None:
        rows = (
            _make_row(
                description="Warn Row",
                transaction_date=date(2026, 7, 1),
                source_row_ref="wd1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-wdet",
            attachment_path="/tmp/fake.pdf",
            parse_statuses={"wd1": "warning"},
            parser_warnings={"wd1": ("low confidence",)},
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-wdet")
        s1 = build_import_run_review_summary_from_import_result(import_result)
        s2 = build_import_run_review_summary_from_import_result(import_result)
        assert s1.warning_reason_counts == s2.warning_reason_counts

    def test_warning_reason_counts_empty_when_no_warnings(self) -> None:
        rows = (
            _make_row(
                description="Clean",
                transaction_date=date(2026, 7, 1),
                source_row_ref="c1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-clean",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-clean2")
        summary = build_import_run_review_summary_from_import_result(import_result)
        assert summary.warning_reason_counts == ()


# ===================================================================
# Evidence source refs
# ===================================================================


class TestEvidenceSourceRefs:
    def test_evidence_refs_in_summary(self) -> None:
        rows = (
            _make_row(
                description="Evidence Test",
                attachment_path="/real/stmt.pdf",
                transaction_date=date(2026, 7, 1),
                source_row_ref="ev1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="pdf-tmpl-cli-abc123",
            attachment_path="/real/stmt.pdf",
            template_id="sample_bank_v1",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-ev")
        summary = build_import_run_review_summary_from_import_result(import_result)
        assert "/real/stmt.pdf" in summary.evidence_source_refs
        assert "pdf-tmpl-cli-abc123" in summary.evidence_source_refs
        assert "template:sample_bank_v1" in summary.evidence_source_refs

    def test_audit_payload_includes_evidence(self) -> None:
        rows = (
            _make_row(
                description="Audit Test",
                attachment_path="/audit/stmt.pdf",
                transaction_date=date(2026, 7, 1),
                source_row_ref="au1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-audit",
            attachment_path="/audit/stmt.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-audit")
        summary = build_import_run_review_summary_from_import_result(import_result)
        audit = export_run_review_summary_audit_payload(summary)
        assert audit["source_pdf_path"] == "/audit/stmt.pdf"
        assert audit["source_statement_id"] == "stmt-audit"
        assert "evidence_source_refs" in audit
        assert len(audit["evidence_source_refs"]) > 0

    def test_source_pdf_path_preserved(self) -> None:
        rows = (
            _make_row(
                description="Path Test",
                attachment_path="/custom/path.pdf",
                transaction_date=date(2026, 7, 1),
                source_row_ref="sp1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-path",
            attachment_path="/custom/path.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-path")
        summary = build_import_run_review_summary_from_import_result(import_result)
        assert summary.source_pdf_path == "/custom/path.pdf"

    def test_source_statement_id_preserved(self) -> None:
        rows = (
            _make_row(
                description="ID Test",
                source_statement_id="pdf-tmpl-cli-xyz",
                transaction_date=date(2026, 7, 1),
                source_row_ref="si1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="pdf-tmpl-cli-xyz",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-sid")
        summary = build_import_run_review_summary_from_import_result(import_result)
        assert summary.source_statement_id == "pdf-tmpl-cli-xyz"


# ===================================================================
# Dashboard-safe output excludes audit-only fields
# ===================================================================


class TestDashboardSafeOutput:
    def test_dashboard_excludes_source_pdf_path(self) -> None:
        rows = (
            _make_row(
                description="Dashboard Test",
                attachment_path="/sensitive/path.pdf",
                transaction_date=date(2026, 7, 1),
                source_row_ref="ds1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-dash",
            attachment_path="/sensitive/path.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-dash")
        summary = build_import_run_review_summary_from_import_result(import_result)
        dashboard = export_run_review_summary_dashboard_payload(summary)
        assert "source_pdf_path" not in dashboard
        assert "source_statement_id" not in dashboard
        assert "evidence_source_refs" not in dashboard

    def test_dashboard_includes_safe_fields(self) -> None:
        rows = (
            _make_row(
                description="Safe Test",
                transaction_date=date(2026, 7, 1),
                source_row_ref="sf1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-safe",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-safe")
        summary = build_import_run_review_summary_from_import_result(import_result)
        dashboard = export_run_review_summary_dashboard_payload(summary)
        assert "run_reference" in dashboard
        assert "run_status" in dashboard
        assert "total_row_count" in dashboard
        assert "blocked_reason_counts" in dashboard
        assert "review_only" in dashboard
        assert "not_final_financial_record" in dashboard

    def test_audit_payload_superset_of_dashboard(self) -> None:
        s = _make_full_summary()
        dashboard = export_run_review_summary_dashboard_payload(s)
        audit = export_run_review_summary_audit_payload(s)
        for key in dashboard:
            assert key in audit


# ===================================================================
# Idempotency
# ===================================================================


class TestIdempotency:
    def test_repeated_summary_stable(self) -> None:
        rows = tuple(
            _make_row(
                description=f"Row {i}",
                transaction_date=date(2026, 7, i),
                source_row_ref=f"id{i}",
            )
            for i in range(1, 4)
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-idem",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-idem")
        s1 = build_import_run_review_summary_from_import_result(import_result)
        s2 = build_import_run_review_summary_from_import_result(import_result)
        assert s1 == s2

    def test_repeated_dashboard_stable(self) -> None:
        s = _make_full_summary()
        d1 = export_run_review_summary_dashboard_payload(s)
        d2 = export_run_review_summary_dashboard_payload(s)
        assert d1 == d2

    def test_repeated_audit_stable(self) -> None:
        s = _make_full_summary()
        a1 = export_run_review_summary_audit_payload(s)
        a2 = export_run_review_summary_audit_payload(s)
        assert a1 == a2

    def test_text_formatter_deterministic(self) -> None:
        s = _make_full_summary()
        t1 = format_import_run_review_summary_text(s)
        t2 = format_import_run_review_summary_text(s)
        assert t1 == t2

    def test_blocked_row_summary_stable(self) -> None:
        rq = _build_blocked_review_queue()
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-bidem")
        s1 = build_import_run_review_summary_from_import_result(import_result)
        s2 = build_import_run_review_summary_from_import_result(import_result)
        assert s1 == s2
        assert s1.blocked_reason_counts == s2.blocked_reason_counts
        assert s1.warning_reason_counts == s2.warning_reason_counts


# ===================================================================
# Cross-batch containment
# ===================================================================


class TestCrossBatchContainment:
    def test_separate_batches_produce_separate_summaries(self) -> None:
        rows_a = (
            _make_row(
                source_statement_id="stmt-a",
                description="Row A",
                transaction_date=date(2026, 7, 1),
                source_row_ref="a1",
            ),
        )
        rq_a = build_pdf_statement_review_queue(
            rows_a,
            source_statement_id="stmt-a",
            attachment_path="/tmp/a.pdf",
        )
        import_a = _build_minimal_import_result(rq_a, batch_public_id="batch-a")
        summary_a = build_import_run_review_summary_from_import_result(import_a)

        rows_b = (
            _make_row(
                source_statement_id="stmt-b",
                description="Row B",
                transaction_date=date(2026, 7, 2),
                source_row_ref="b1",
            ),
        )
        rq_b = build_pdf_statement_review_queue(
            rows_b,
            source_statement_id="stmt-b",
            attachment_path="/tmp/b.pdf",
        )
        import_b = _build_minimal_import_result(rq_b, batch_public_id="batch-b")
        summary_b = build_import_run_review_summary_from_import_result(import_b)

        assert summary_a.run_reference == "batch-a"
        assert summary_b.run_reference == "batch-b"
        assert summary_a.source_statement_id == "stmt-a"
        assert summary_b.source_statement_id == "stmt-b"
        assert summary_a.source_pdf_path != summary_b.source_pdf_path

    def test_batch_public_id_is_deterministic(self) -> None:
        rows = (
            _make_row(
                description="Test",
                transaction_date=date(2026, 7, 1),
                source_row_ref="bd1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-bd",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="my-batch")
        s1 = build_import_run_review_summary_from_import_result(import_result)
        s2 = build_import_run_review_summary_from_import_result(import_result)
        assert s1.batch_public_id == s2.batch_public_id
        assert s1.run_reference == s2.run_reference


# ===================================================================
# Empty run behavior
# ===================================================================


class TestEmptyRunBehavior:
    def test_empty_run_fully_ready(self) -> None:
        rows: tuple[ParsedPdfStatementRow, ...] = ()
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-empty",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-empty")
        summary = build_import_run_review_summary_from_import_result(import_result)
        assert summary.run_status == "fully_ready"
        assert summary.total_row_count == 0
        assert summary.ready_for_import_count == 0
        assert summary.blocked_reason_counts == ()
        assert summary.warning_reason_counts == ()

    def test_empty_run_dashboard_does_not_crash(self) -> None:
        s = _make_summary(
            total_row_count=0,
            ready_for_import_count=0,
            run_status="fully_ready",
        )
        dashboard = export_run_review_summary_dashboard_payload(s)
        assert dashboard["total_row_count"] == 0
        assert dashboard["run_status"] == "fully_ready"

    def test_empty_run_audit_does_not_crash(self) -> None:
        s = _make_summary(total_row_count=0, ready_for_import_count=0)
        audit = export_run_review_summary_audit_payload(s)
        assert audit["total_row_count"] == 0


# ===================================================================
# Source mode handling
# ===================================================================


class TestSourceModeHandling:
    def test_fixture_text_mode(self) -> None:
        rows = (
            _make_row(
                description="Fixture",
                transaction_date=date(2026, 7, 1),
                source_row_ref="ft1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-ft",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-ft")
        summary = build_import_run_review_summary_from_import_result(
            import_result, source_mode="fixture_text"
        )
        assert summary.source_mode == "fixture_text"
        assert summary.pdf_parsing_mode == "fixture_text"

    def test_pdf_text_mode(self) -> None:
        rows = (
            _make_row(
                description="PDF Text",
                transaction_date=date(2026, 7, 1),
                source_row_ref="pt1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-pt",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(
            rq, batch_public_id="batch-pt", pdf_parsing_mode="pdf_text"
        )
        summary = build_import_run_review_summary_from_import_result(
            import_result, source_mode="pdf_text"
        )
        assert summary.source_mode == "pdf_text"
        assert summary.pdf_parsing_mode == "pdf_text"

    def test_source_mode_defaults_to_pdf_parsing_mode(self) -> None:
        rows = (
            _make_row(
                description="Default",
                transaction_date=date(2026, 7, 1),
                source_row_ref="sd1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-sd",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-sd")
        summary = build_import_run_review_summary_from_import_result(import_result)
        assert summary.source_mode == summary.pdf_parsing_mode


# ===================================================================
# Guard flags
# ===================================================================


class TestReviewOnlyGuards:
    def test_guard_flags_true_ready(self) -> None:
        s = _make_full_summary(run_status="fully_ready")
        assert s.review_only is True
        assert s.not_final_financial_record is True

    def test_guard_flags_true_blocked(self) -> None:
        s = _make_full_summary(run_status="blocked")
        assert s.review_only is True
        assert s.not_final_financial_record is True

    def test_guard_flags_in_dashboard(self) -> None:
        s = _make_full_summary()
        dashboard = export_run_review_summary_dashboard_payload(s)
        assert dashboard["review_only"] is True
        assert dashboard["not_final_financial_record"] is True

    def test_guard_flags_in_audit(self) -> None:
        s = _make_full_summary()
        audit = export_run_review_summary_audit_payload(s)
        assert audit["review_only"] is True
        assert audit["not_final_financial_record"] is True


# ===================================================================
# No live DB / no unsafe modules
# ===================================================================


class TestNoLiveDatabaseReference:
    def test_no_live_db_path(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_review_summary as mod

        source = inspect.getsource(mod)
        assert "finance.db" not in source

    def test_no_sqlite3_import(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_review_summary as mod

        source = inspect.getsource(mod)
        assert "sqlite3" not in source

    def test_no_pdf_ocr_dependencies(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_review_summary as mod

        source = inspect.getsource(mod)
        source_lower = source.lower()
        for dep in ("pdfplumber", "pytesseract", "pypdf", "camelot", "tabula"):
            assert dep not in source_lower, f"Unexpected dependency: {dep}"

    def test_no_file_io(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_review_summary as mod

        source = inspect.getsource(mod)
        assert "open(" not in source

    def test_no_persistence_imports(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_review_summary as mod

        source = inspect.getsource(mod)
        assert "from .persistence" not in source
        assert "from finance_core.reconciliation.persistence" not in source

    def test_no_statement_import(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_review_summary as mod

        source = inspect.getsource(mod)
        assert "statement_import" not in source.lower()

    def test_no_apply_import(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_review_summary as mod

        source = inspect.getsource(mod)
        assert "from .apply" not in source
        assert "finance_core.reconciliation.apply" not in source

    def test_no_telegram_import_in_code(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_review_summary as mod

        source = inspect.getsource(mod)
        # Remove docstring — non-goals mention these terms legitimately
        code_only = source.split("Non-goals:")[0] if "Non-goals:" in source else source
        assert "telegram" not in code_only.lower()

    def test_no_ocr_runtime_in_code(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_review_summary as mod

        source = inspect.getsource(mod)
        code_only = source.split("Non-goals:")[0] if "Non-goals:" in source else source
        assert "ocr" not in code_only.lower()

    def test_no_settlement_import_in_code(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_review_summary as mod

        source = inspect.getsource(mod)
        code_only = source.split("Non-goals:")[0] if "Non-goals:" in source else source
        assert "settlement" not in code_only.lower()


# ===================================================================
# Text formatter tests
# ===================================================================


class TestTextFormatter:
    def test_formatter_includes_run_status(self) -> None:
        s = _make_full_summary(run_status="fully_ready")
        text = format_import_run_review_summary_text(s)
        assert "fully_ready" in text
        assert "PDF Statement Import Run Review Summary" in text

    def test_formatter_includes_counts(self) -> None:
        s = _make_full_summary()
        text = format_import_run_review_summary_text(s)
        assert "Total rows:" in text
        assert "Ready for import:" in text
        assert "Needs review:" in text
        assert "Blocked:" in text

    def test_formatter_includes_guard_flags(self) -> None:
        s = _make_full_summary()
        text = format_import_run_review_summary_text(s)
        assert "review_only: True" in text
        assert "not_final_financial_record: True" in text

    def test_formatter_includes_source_info(self) -> None:
        s = _make_full_summary()
        text = format_import_run_review_summary_text(s)
        assert s.source_pdf_path in text
        assert s.source_statement_id in text
        assert s.source_mode in text

    def test_formatter_includes_blocked_reason_breakdown(self) -> None:
        rq = _build_blocked_review_queue()
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-fmt-b")
        summary = build_import_run_review_summary_from_import_result(import_result)
        text = format_import_run_review_summary_text(summary)
        assert "Blocked reason breakdown:" in text

    def test_formatter_includes_warning_reason_breakdown(self) -> None:
        rows = (
            _make_row(
                description="Warn Row",
                transaction_date=date(2026, 7, 1),
                source_row_ref="wf1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-wfmt",
            attachment_path="/tmp/fake.pdf",
            parse_statuses={"wf1": "warning"},
            parser_warnings={"wf1": ("low confidence",)},
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-wfmt")
        summary = build_import_run_review_summary_from_import_result(import_result)
        text = format_import_run_review_summary_text(summary)
        assert "Warning reason breakdown:" in text

    def test_formatter_no_blocked_section_when_none(self) -> None:
        s = _make_full_summary(run_status="fully_ready")
        text = format_import_run_review_summary_text(s)
        assert "Blocked reason breakdown:" not in text

    def test_formatter_includes_evidence_refs(self) -> None:
        s = _make_full_summary()
        text = format_import_run_review_summary_text(s)
        assert "Evidence source refs:" in text


# ===================================================================
# Persistence info
# ===================================================================


class TestPersistenceInfo:
    def test_persisted_count_zero_by_default(self) -> None:
        s = _make_full_summary()
        assert s.persisted_count == 0
        assert s.persistence_run_public_id is None

    def test_persisted_count_carried(self) -> None:
        rows = (
            _make_row(
                description="Persist Test",
                transaction_date=date(2026, 7, 1),
                source_row_ref="pi1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-pi",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-pi")
        summary = build_import_run_review_summary_from_import_result(
            import_result, persisted_count=5, persistence_run_public_id="run-pub-123"
        )
        assert summary.persisted_count == 5
        assert summary.persistence_run_public_id == "run-pub-123"

    def test_persisted_count_zero_hidden_in_dashboard(self) -> None:
        s = _make_full_summary()
        dashboard = export_run_review_summary_dashboard_payload(s)
        assert "persisted_count" not in dashboard

    def test_persisted_count_in_dashboard_when_positive(self) -> None:
        rows = (
            _make_row(
                description="P Test",
                transaction_date=date(2026, 7, 1),
                source_row_ref="pp1",
            ),
        )
        rq = build_pdf_statement_review_queue(
            rows,
            source_statement_id="stmt-pp",
            attachment_path="/tmp/fake.pdf",
        )
        import_result = _build_minimal_import_result(rq, batch_public_id="batch-pp")
        summary = build_import_run_review_summary_from_import_result(
            import_result, persisted_count=3
        )
        dashboard = export_run_review_summary_dashboard_payload(summary)
        assert dashboard["persisted_count"] == 3


# ===================================================================
# Integration tests via fixture import pipeline
# ===================================================================


class TestIntegrationWithFixtureImport:
    def test_integration_fixture_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_import.db"
            import_result = import_pdf_statement_fixture_to_temp_db(
                db_path=str(db_path),
                pdf_path=str(DEFAULT_PDF_FIXTURE_PATH),
                text_fixture_path=str(DEFAULT_TEXT_FIXTURE_PATH),
                template_id=DEFAULT_TEMPLATE_ID,
                batch_public_id="test-int-batch",
                source_mode="fixture_text",
            )
            summary = build_import_run_review_summary_from_import_result(
                import_result, source_mode="fixture_text"
            )
            assert summary.total_row_count == 3
            assert summary.ready_for_import_count >= 0
            assert summary.run_status in ("fully_ready", "partially_reviewable", "blocked")
            assert summary.source_mode == "fixture_text"
            assert summary.pdf_parsing_mode == "fixture_text"
            assert summary.batch_public_id == "test-int-batch"
            assert summary.review_only is True
            assert summary.not_final_financial_record is True

    def test_integration_dashboard_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_dash.db"
            import_result = import_pdf_statement_fixture_to_temp_db(
                db_path=str(db_path),
                batch_public_id="test-dash-batch",
                source_mode="fixture_text",
            )
            summary = build_import_run_review_summary_from_import_result(
                import_result,
                source_mode="fixture_text",
            )
            dashboard = export_run_review_summary_dashboard_payload(summary)
            assert "run_status" in dashboard
            assert "total_row_count" in dashboard
            assert "blocked_reason_counts" in dashboard
            assert "source_pdf_path" not in dashboard

    def test_integration_audit_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_audit.db"
            import_result = import_pdf_statement_fixture_to_temp_db(
                db_path=str(db_path),
                batch_public_id="test-audit-batch",
                source_mode="fixture_text",
            )
            summary = build_import_run_review_summary_from_import_result(
                import_result,
                source_mode="fixture_text",
            )
            audit = export_run_review_summary_audit_payload(summary)
            assert "source_pdf_path" in audit
            assert "source_statement_id" in audit
            assert "evidence_source_refs" in audit

    def test_integration_text_formatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_fmt.db"
            import_result = import_pdf_statement_fixture_to_temp_db(
                db_path=str(db_path),
                batch_public_id="test-fmt-batch",
                source_mode="fixture_text",
            )
            summary = build_import_run_review_summary_from_import_result(
                import_result,
                source_mode="fixture_text",
            )
            text = format_import_run_review_summary_text(summary)
            assert "PDF Statement Import Run Review Summary" in text
            assert "review_only: True" in text


# ===================================================================
# Integration via bridge result
# ===================================================================


class TestIntegrationWithBridgeResult:
    def test_bridge_result_to_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_bridge.db"
            bridge_result = run_pdf_statement_review_queue_bridge(
                db_path=str(db_path),
                source_mode="fixture_text",
                batch_public_id="test-bridge-batch",
            )
            summary = build_import_run_review_summary_from_bridge_result(bridge_result)
            assert summary.run_status in ("fully_ready", "partially_reviewable", "blocked")
            assert summary.source_mode == "fixture_text"
            assert summary.batch_public_id == "test-bridge-batch"
            assert summary.review_only is True
            assert summary.not_final_financial_record is True

    def test_bridge_result_carries_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_bridge_persist.db"
            bridge_result = run_pdf_statement_review_queue_bridge(
                db_path=str(db_path),
                source_mode="fixture_text",
                batch_public_id="test-persist-batch",
                persist_review_queue=True,
                persistence_run_public_id="persist-run-1",
            )
            summary = build_import_run_review_summary_from_bridge_result(bridge_result)
            assert summary.persisted_count == bridge_result.persisted_count
            assert summary.persistence_run_public_id == "persist-run-1"


# ===================================================================
# Public API shape
# ===================================================================


class TestPublicAPI:
    def test_all_exports(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_review_summary as mod

        assert hasattr(mod, "__all__")
        expected = {
            "PdfStatementImportRunReviewSummary",
            "RunReviewStatus",
            "build_import_run_review_summary_from_import_result",
            "build_import_run_review_summary_from_bridge_result",
            "export_run_review_summary_dashboard_payload",
            "export_run_review_summary_audit_payload",
            "format_import_run_review_summary_text",
        }
        assert set(mod.__all__) == expected

    def test_summary_type(self) -> None:
        s = _make_full_summary()
        assert isinstance(s, PdfStatementImportRunReviewSummary)
        assert isinstance(s.run_status, str)
        assert s.run_status in ("fully_ready", "partially_reviewable", "blocked")
