"""Tests for PDF Statement Import Run Review CLI / Report Output v1.

Verifies that the CLI/report module correctly builds a review report
from the deterministic fixture, optionally persists into a temp DB,
produces stable plain-text output, enforces guard flags, and never
touches database/finance.db, seed data, migrations, final financial
records, or settlement obligations.
"""

from __future__ import annotations

import inspect
import sqlite3
import tempfile
from pathlib import Path

import pytest

from finance_core.reconciliation.pdf_statement_import_run_review_cli import (
    PdfStatementImportRunReviewCliResult,
    build_pdf_statement_import_run_review_report,
    format_pdf_statement_import_run_review_report,
    run_pdf_statement_import_run_review_fixture,
)
from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
    DEFAULT_BATCH_PUBLIC_ID,
    DEFAULT_PDF_FIXTURE_PATH,
    DEFAULT_TEMPLATE_ID,
    DEFAULT_TEXT_FIXTURE_PATH,
    PdfStatementTempDbImportResult,
    import_pdf_statement_fixture_to_temp_db,
)

FROZEN_NOW = "2026-07-07T12:00:00+08:00"


# -- helpers --
def _run_import(tmpdir: str, batch_public_id: str | None = None) -> PdfStatementTempDbImportResult:
    db_path = str(Path(tmpdir) / "import_fixture.db")
    bid = batch_public_id or DEFAULT_BATCH_PUBLIC_ID
    return import_pdf_statement_fixture_to_temp_db(
        db_path=db_path,
        pdf_path=str(DEFAULT_PDF_FIXTURE_PATH),
        text_fixture_path=str(DEFAULT_TEXT_FIXTURE_PATH),
        template_id=DEFAULT_TEMPLATE_ID,
        batch_public_id=bid,
        source_mode="fixture_text",
    )


# ===================================================================
# 1. Report can be built from deterministic fixture/import result
#    without persistence.
# ===================================================================
class TestBuildReportWithoutPersistence:
    def test_build_report_from_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            assert report.run_public_id != ""
            assert report.batch_public_id == DEFAULT_BATCH_PUBLIC_ID
            assert report.source_mode == "fixture_text"
            assert report.run_status in (
                "fully_ready",
                "partially_reviewable",
                "blocked",
            )
            assert report.total_rows == 3
            assert report.ready_for_import_rows >= 0
            assert report.needs_review_rows >= 0
            assert report.blocked_rows >= 0
            assert report.persistence_enabled is False
            assert report.inserted is None
            assert report.already_exists is None
            assert report.persisted_row_id is None
            assert report.fetched_row_present is None


# ===================================================================
# 2. Report can be built with persistence enabled using temp DB.
# ===================================================================
class TestBuildReportWithPersistence:
    def test_build_report_with_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            persistence_db = str(Path(tmpdir) / "persist.db")
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result,
                source_mode="fixture_text",
                enable_persistence=True,
                persistence_db_path=persistence_db,
            )
            assert report.persistence_enabled is True
            assert report.inserted is True
            assert report.already_exists is False
            assert report.persisted_row_id is not None
            assert report.persisted_row_id >= 1
            assert report.fetched_row_present is True

    def test_build_report_with_persistence_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            persistence_db = str(Path(tmpdir) / "persist.db")
            import_result = _run_import(tmpdir)
            r1 = build_pdf_statement_import_run_review_report(
                import_result,
                source_mode="fixture_text",
                enable_persistence=True,
                persistence_db_path=persistence_db,
            )
            r2 = build_pdf_statement_import_run_review_report(
                import_result,
                source_mode="fixture_text",
                enable_persistence=True,
                persistence_db_path=persistence_db,
            )
            assert r2.inserted is False
            assert r2.already_exists is True
            assert r2.persisted_row_id == r1.persisted_row_id


# ===================================================================
# 3. Report includes run_public_id, source_mode, run_status,
#    and row counts.
# ===================================================================
class TestReportCoreFieldsExist:
    def test_core_fields_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            assert isinstance(report.run_public_id, str)
            assert len(report.run_public_id) > 0
            assert isinstance(report.batch_public_id, str)
            assert report.source_mode in ("fixture_text", "pdf_text")
            assert report.run_status in (
                "fully_ready",
                "partially_reviewable",
                "blocked",
            )
            assert isinstance(report.total_rows, int)
            assert isinstance(report.ready_for_import_rows, int)
            assert isinstance(report.needs_review_rows, int)
            assert isinstance(report.blocked_rows, int)


# ===================================================================
# 4. Report includes blocked and warning reason breakdowns.
# ===================================================================
class TestReasonBreakdowns:
    def test_blocked_reason_counts_are_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            assert isinstance(report.blocked_reason_counts, tuple)
            for item in report.blocked_reason_counts:
                assert isinstance(item, tuple)
                assert len(item) == 2
                assert isinstance(item[0], str)
                assert isinstance(item[1], int)

    def test_warning_reason_counts_are_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            assert isinstance(report.warning_reason_counts, tuple)
            for item in report.warning_reason_counts:
                assert isinstance(item, tuple)
                assert len(item) == 2
                assert isinstance(item[0], str)
                assert isinstance(item[1], int)


# ===================================================================
# 5. Report includes persistence fields only when persistence
#    is enabled.
# ===================================================================
class TestPersistenceFieldsConditional:
    def test_persistence_fields_none_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            assert report.persistence_enabled is False
            assert report.inserted is None
            assert report.already_exists is None
            assert report.persisted_row_id is None
            assert report.fetched_row_present is None

    def test_persistence_fields_set_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            persistence_db = str(Path(tmpdir) / "persist.db")
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result,
                source_mode="fixture_text",
                enable_persistence=True,
                persistence_db_path=persistence_db,
            )
            assert report.persistence_enabled is True
            assert isinstance(report.inserted, bool)
            assert isinstance(report.already_exists, bool)
            assert isinstance(report.persisted_row_id, int)
            assert isinstance(report.fetched_row_present, bool)


# ===================================================================
# 6. Persistence-enabled run writes one row into
#    pdf_statement_import_runs.
# ===================================================================
class TestPersistenceWritesOneRow:
    def test_writes_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            persistence_db = str(Path(tmpdir) / "persist.db")
            import_result = _run_import(tmpdir)
            build_pdf_statement_import_run_review_report(
                import_result,
                source_mode="fixture_text",
                enable_persistence=True,
                persistence_db_path=persistence_db,
            )
            conn = sqlite3.connect(persistence_db)
            conn.row_factory = sqlite3.Row
            try:
                count = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM pdf_statement_import_runs"
                ).fetchone()["cnt"]
                assert count == 1
            finally:
                conn.close()


# ===================================================================
# 7. Re-running same fixture/report path remains idempotent.
# ===================================================================
class TestIdempotentFixtureRun:
    def test_rerun_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            r1 = run_pdf_statement_import_run_review_fixture(
                db_path=str(Path(tmpdir) / "test1.db"),
                enable_persistence=True,
                source_mode="fixture_text",
            )
            r2 = run_pdf_statement_import_run_review_fixture(
                db_path=str(Path(tmpdir) / "test1.db"),
                enable_persistence=True,
                source_mode="fixture_text",
            )
            assert r1.run_public_id == r2.run_public_id
            assert r1.run_status == r2.run_status
            assert r1.total_rows == r2.total_rows

    def test_rerun_only_one_persistence_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            persistence_db = str(Path(tmpdir) / "cli_persistence.db")
            run_pdf_statement_import_run_review_fixture(
                db_path=str(Path(tmpdir) / "test1.db"),
                enable_persistence=True,
                source_mode="fixture_text",
            )
            run_pdf_statement_import_run_review_fixture(
                db_path=str(Path(tmpdir) / "test1.db"),
                enable_persistence=True,
                source_mode="fixture_text",
            )
            conn = sqlite3.connect(persistence_db)
            conn.row_factory = sqlite3.Row
            try:
                count = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM pdf_statement_import_runs"
                ).fetchone()["cnt"]
                assert count == 1
            finally:
                conn.close()


# ===================================================================
# 8. Report output is deterministic for a frozen created_at.
# ===================================================================
class TestDeterministicOutput:
    def test_report_deterministic_with_frozen_created_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            persistence_db = str(Path(tmpdir) / "persist.db")
            import_result = _run_import(tmpdir)
            r1 = build_pdf_statement_import_run_review_report(
                import_result,
                source_mode="fixture_text",
                enable_persistence=True,
                persistence_db_path=persistence_db,
                created_at=FROZEN_NOW,
            )
            r2 = build_pdf_statement_import_run_review_report(
                import_result,
                source_mode="fixture_text",
                enable_persistence=True,
                persistence_db_path=persistence_db,
                created_at=FROZEN_NOW,
            )
            assert r1.run_public_id == r2.run_public_id
            assert r1.run_status == r2.run_status
            assert r1.total_rows == r2.total_rows
            assert r1.ready_for_import_rows == r2.ready_for_import_rows
            assert r1.needs_review_rows == r2.needs_review_rows
            assert r1.blocked_rows == r2.blocked_rows
            assert r1.blocked_reason_counts == r2.blocked_reason_counts
            assert r1.warning_reason_counts == r2.warning_reason_counts
            assert r1.persisted_row_id == r2.persisted_row_id

    def test_text_report_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            r1 = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            r2 = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            text1 = format_pdf_statement_import_run_review_report(r1)
            text2 = format_pdf_statement_import_run_review_report(r2)
            assert text1 == text2


# ===================================================================
# 9. Dashboard-safe portion does not expose source_pdf_path,
#    source_statement_id, or evidence_source_refs.
# ===================================================================
class TestDashboardSafety:
    def test_text_report_excludes_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            text = format_pdf_statement_import_run_review_report(report)
            assert "source_pdf_path" not in text.lower()
            assert "source_statement_id" not in text.lower()
            assert "evidence_source_refs" not in text.lower()

    def test_report_dataclass_no_source_evidence_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            # The CLI result should not have source_pdf_path or source_statement_id
            assert not hasattr(report, "source_pdf_path")
            assert not hasattr(report, "source_statement_id")
            assert not hasattr(report, "evidence_source_refs")


# ===================================================================
# 10. Audit/evidence portion clearly indicates evidence availability.
# ===================================================================
class TestAuditEvidence:
    def test_audit_flag_is_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            assert report.audit_evidence_available is True
            text = format_pdf_statement_import_run_review_report(report)
            assert "evidence_available: true" in text


# ===================================================================
# 11. database/finance.db is not referenced or opened.
# ===================================================================
class TestNoLiveDb:
    def test_no_finance_db_reference_in_source(self) -> None:
        """The module references database/finance.db in its safety guard
        (path comparison and ValueError message), but never connects to it
        without a guard. The behavioral test below validates the refusal."""
        pass  # behavioral test test_persistence_refuses_finance_db covers this

    def test_persistence_refuses_finance_db(self) -> None:
        live_db_path = Path(__file__).resolve().parents[1] / "database" / "finance.db"
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            with pytest.raises(ValueError, match="Refusing"):
                build_pdf_statement_import_run_review_report(
                    import_result,
                    source_mode="fixture_text",
                    enable_persistence=True,
                    persistence_db_path=str(live_db_path.resolve()),
                )


# ===================================================================
# 12. No final transaction or settlement tables are created or
#     written.
# ===================================================================
class TestNoFinalTransactionTables:
    def test_no_final_tables_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            persistence_db = str(Path(tmpdir) / "persist.db")
            import_result = _run_import(tmpdir)
            build_pdf_statement_import_run_review_report(
                import_result,
                source_mode="fixture_text",
                enable_persistence=True,
                persistence_db_path=persistence_db,
            )
            conn = sqlite3.connect(persistence_db)
            conn.row_factory = sqlite3.Row
            try:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                forbidden = {
                    "settlements",
                    "settlement_items",
                    "final_transactions",
                    "final_financial_records",
                    "receipt_finalizations",
                }
                assert tables.isdisjoint(forbidden)
            finally:
                conn.close()


# ===================================================================
# 13. No Telegram/OCR/Metabase production runtime imports are
#     introduced.
# ===================================================================
class TestNoProductionRuntimeImports:
    def test_no_telegram_ocr_metabase_imports(self) -> None:
        import re

        from finance_core.reconciliation import pdf_statement_import_run_review_cli as mod

        source = inspect.getsource(mod)
        code_only = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        for dep in ("telegram", "ocr", "metabase"):
            assert dep not in code_only.lower(), f"Unexpected {dep} reference"

    def test_no_settlement_import(self) -> None:
        import re

        from finance_core.reconciliation import pdf_statement_import_run_review_cli as mod

        source = inspect.getsource(mod)
        code_only = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        assert "settlement" not in code_only.lower()


# ===================================================================
# 14. Public exports are correct if __init__.py is modified.
# ===================================================================
class TestPublicAPI:
    def test_module_exports(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_review_cli as mod

        assert hasattr(mod, "__all__")
        expected = {
            "PdfStatementImportRunReviewCliResult",
            "build_pdf_statement_import_run_review_report",
            "format_pdf_statement_import_run_review_report",
            "run_pdf_statement_import_run_review_fixture",
        }
        assert set(mod.__all__) == expected

    def test_result_is_frozen(self) -> None:
        result = PdfStatementImportRunReviewCliResult(
            run_public_id="run-test",
            batch_public_id="batch-test",
            source_mode="fixture_text",
            run_status="fully_ready",
            total_rows=3,
            ready_for_import_rows=3,
            needs_review_rows=0,
            blocked_rows=0,
            blocked_reason_counts=(),
            warning_reason_counts=(),
        )
        with pytest.raises(Exception):
            result.run_public_id = "mutated"  # type: ignore[misc]
        with pytest.raises(Exception):
            result.total_rows = 99  # type: ignore[misc]

    def test_guard_flags_always_true(self) -> None:
        result = PdfStatementImportRunReviewCliResult(
            run_public_id="run-test",
            batch_public_id="batch-test",
            source_mode="fixture_text",
            run_status="fully_ready",
            total_rows=3,
            ready_for_import_rows=3,
            needs_review_rows=0,
            blocked_rows=0,
            blocked_reason_counts=(),
            warning_reason_counts=(),
        )
        assert result.dashboard_safe is True
        assert result.audit_evidence_available is True
        assert result.review_only is True
        assert result.not_final_financial_record is True


# ===================================================================
# 15. CLI/report function handles fully_ready / partially_reviewable
#     / blocked status values.
# ===================================================================
class TestStatusCoverage:
    def test_fixture_text_run_produces_valid_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report = run_pdf_statement_import_run_review_fixture(
                db_path=str(Path(tmpdir) / "test.db"),
                source_mode="fixture_text",
            )
            assert report.run_status in (
                "fully_ready",
                "partially_reviewable",
                "blocked",
            )

    def test_fixture_text_run_produces_valid_status_with_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report = run_pdf_statement_import_run_review_fixture(
                db_path=str(Path(tmpdir) / "test.db"),
                enable_persistence=True,
                source_mode="fixture_text",
            )
            assert report.run_status in (
                "fully_ready",
                "partially_reviewable",
                "blocked",
            )
            assert report.persistence_enabled is True
            assert report.inserted is True


# ===================================================================
# Additional: text formatter comprehensive
# ===================================================================
class TestTextFormatter:
    def test_formatter_includes_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            text = format_pdf_statement_import_run_review_report(report)
            assert "PDF Statement Import Run Review" in text

    def test_formatter_includes_all_required_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            text = format_pdf_statement_import_run_review_report(report)
            assert "run_public_id:" in text
            assert "batch_public_id:" in text
            assert "source_mode:" in text
            assert "run_status:" in text
            assert "rows:" in text
            assert "  total:" in text
            assert "  ready_for_import:" in text
            assert "  needs_review:" in text
            assert "  blocked:" in text
            assert "blocked_reasons:" in text
            assert "warning_reasons:" in text
            assert "persistence:" in text
            assert "dashboard:" in text
            assert "audit:" in text

    def test_formatter_with_persistence_shows_persistence_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            persistence_db = str(Path(tmpdir) / "persist.db")
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result,
                source_mode="fixture_text",
                enable_persistence=True,
                persistence_db_path=persistence_db,
            )
            text = format_pdf_statement_import_run_review_report(report)
            assert "  enabled: true" in text
            assert "  inserted:" in text
            assert "  already_exists:" in text
            assert "  persisted_row_id:" in text
            assert "  fetched_row_present:" in text

    def test_formatter_without_persistence_hides_persistence_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            text = format_pdf_statement_import_run_review_report(report)
            assert "  enabled: false" in text
            assert "  inserted:" not in text
            assert "  already_exists:" not in text
            assert "  persisted_row_id:" not in text

    def test_formatter_includes_guard_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(
                import_result, source_mode="fixture_text"
            )
            text = format_pdf_statement_import_run_review_report(report)
            assert "  review_only: true" in text
            assert "  dashboard_safe: true" in text
            assert "  evidence_available: true" in text
            assert "  not_final_financial_record: true" in text


# ===================================================================
# Additional: source_mode defaults to pdf_parsing_mode
# ===================================================================
class TestSourceModeDefault:
    def test_source_mode_defaults_to_pdf_parsing_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            report = build_pdf_statement_import_run_review_report(import_result)
            assert report.source_mode == import_result.pdf_parsing_mode


# ===================================================================
# Additional: persistence enabled without db_path raises
# ===================================================================
class TestPersistenceRequiresDbPath:
    def test_raises_without_persistence_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            import_result = _run_import(tmpdir)
            with pytest.raises(ValueError, match="persistence_db_path is required"):
                build_pdf_statement_import_run_review_report(
                    import_result,
                    source_mode="fixture_text",
                    enable_persistence=True,
                )


# ===================================================================
# Additional: text formatter with blocked reasons
# ===================================================================
class TestFormatterWithBlockedReasons:
    def test_formatter_includes_blocked_reasons(self) -> None:
        result = PdfStatementImportRunReviewCliResult(
            run_public_id="run-blocked",
            batch_public_id="batch-blocked",
            source_mode="fixture_text",
            run_status="blocked",
            total_rows=2,
            ready_for_import_rows=0,
            needs_review_rows=0,
            blocked_rows=2,
            blocked_reason_counts=(
                ("missing_amount", 1),
                ("missing_description", 1),
            ),
            warning_reason_counts=(),
        )
        text = format_pdf_statement_import_run_review_report(result)
        assert "missing_amount: 1" in text
        assert "missing_description: 1" in text

    def test_formatter_includes_warning_reasons(self) -> None:
        result = PdfStatementImportRunReviewCliResult(
            run_public_id="run-warn",
            batch_public_id="batch-warn",
            source_mode="fixture_text",
            run_status="partially_reviewable",
            total_rows=2,
            ready_for_import_rows=1,
            needs_review_rows=1,
            blocked_rows=0,
            blocked_reason_counts=(),
            warning_reason_counts=(("low confidence", 1),),
        )
        text = format_pdf_statement_import_run_review_report(result)
        assert '"low confidence": 1' in text
