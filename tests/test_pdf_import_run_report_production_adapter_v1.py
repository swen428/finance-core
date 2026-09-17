"""Tests for PDF Import Run Report Production Adapter Interface v1.

Covers adapter input/source types, builder functions, guard flags,
dashboard/audit payload separation, source-evidence preservation,
existing builder compatibility, determinism, and safety guarantees.
Every test is side-effect-free -- no live DB access, no mutations.
"""

from __future__ import annotations

import inspect
import json
import re
import tempfile
from pathlib import Path

import pytest

from finance_core.reconciliation.pdf_import_run_report import (
    PdfImportRunIssues,
    PdfImportRunReport,
    export_pdf_import_run_report_audit_payload,
    export_pdf_import_run_report_dashboard_payload,
    format_pdf_import_run_report_text,
)
from finance_core.reconciliation.pdf_import_run_report_production_adapter import (
    PdfImportRunReportProductionAdapterInput,
    PdfImportRunReportProductionAdapterSource,
    build_pdf_import_run_report_from_production_bridge_result,
    build_pdf_import_run_report_from_production_import_result,
    build_pdf_import_run_report_production_adapter_from_bridge_result,
    build_pdf_import_run_report_production_adapter_from_import_result,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_input(**overrides):
    kwargs = dict(
        parsed_rows=50,
        accepted_rows=48,
        imported_rows=45,
        skipped_duplicate_rows=2,
        idempotent_rows=1,
        blocked_rows=0,
        needs_review_rows=3,
        ready_for_import_rows=45,
    )
    kwargs.update(overrides)
    return PdfImportRunReportProductionAdapterInput(**kwargs)


def _make_source(**overrides):
    kwargs = dict(
        source_pdf_path="/data/statements/2025-01.pdf",
        source_statement_id="stmt-prod-abc-001",
        attachment_path="/data/attachments/abc-001.pdf",
        template_id="bank_abc_v2",
        source_mode="pdf_text",
        source_type="bank_statement",
    )
    kwargs.update(overrides)
    return PdfImportRunReportProductionAdapterSource(**kwargs)


def _make_issues(**overrides):
    kwargs = dict(
        blocked_reason_counts=(),
        parser_warning_counts=(),
        total_blocks=0,
        total_warnings=0,
    )
    kwargs.update(overrides)
    return PdfImportRunIssues(**kwargs)


# ---------------------------------------------------------------------------
# Adapter input and source dataclass tests
# ---------------------------------------------------------------------------


class TestAdapterInputDataclass:
    def test_default_construction(self):
        inp = _make_input()
        assert inp.parsed_rows == 50
        assert inp.ready_for_import_rows == 45

    def test_frozen(self):
        inp = _make_input()
        with pytest.raises(Exception):
            inp.parsed_rows = 99  # type: ignore[misc]

    def test_all_zeros(self):
        inp = PdfImportRunReportProductionAdapterInput(
            parsed_rows=0,
            accepted_rows=0,
            imported_rows=0,
            skipped_duplicate_rows=0,
            idempotent_rows=0,
            blocked_rows=0,
            needs_review_rows=0,
            ready_for_import_rows=0,
        )
        assert inp.parsed_rows == 0

    def test_equality(self):
        a = _make_input()
        b = _make_input()
        assert a == b
        c = _make_input(parsed_rows=999)
        assert a != c


class TestAdapterSourceDataclass:
    def test_default_construction(self):
        src = _make_source()
        assert src.source_pdf_path == "/data/statements/2025-01.pdf"
        assert src.source_type == "bank_statement"

    def test_frozen(self):
        src = _make_source()
        with pytest.raises(Exception):
            src.source_pdf_path = "/other"  # type: ignore[misc]

    def test_none_template_id(self):
        src = _make_source(template_id=None)
        assert src.template_id is None


# ---------------------------------------------------------------------------
# Builder: production import result
# ---------------------------------------------------------------------------


class TestBuildFromProductionImportResult:
    def test_builds_report(self):
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-001",
        )
        assert isinstance(report, PdfImportRunReport)
        assert report.run_reference == "batch-prod-001"

    def test_source_evidence_preserved(self):
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-002",
        )
        se = report.source_evidence
        assert se.source_pdf_path == "/data/statements/2025-01.pdf"
        assert se.source_statement_id == "stmt-prod-abc-001"
        assert se.attachment_path == "/data/attachments/abc-001.pdf"
        assert se.template_id == "bank_abc_v2"
        assert se.source_mode == "pdf_text"
        assert se.source_type == "bank_statement"

    def test_outcomes_mapped(self):
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-003",
        )
        oc = report.outcomes
        assert oc.parsed_rows == 50
        assert oc.imported_rows == 45
        assert oc.blocked_rows == 0
        assert oc.needs_review_rows == 3
        assert oc.ready_for_import_rows == 45

    def test_guard_flags_always_true(self):
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-004",
        )
        assert report.review_only is True
        assert report.not_final_financial_record is True

    def test_reconciliation_readiness_ready(self):
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(blocked_rows=0),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-005",
        )
        assert report.reconciliation_readiness.is_ready is True
        assert report.reconciliation_readiness.reason == "ready_with_review_items"

    def test_reconciliation_readiness_blocked(self):
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(blocked_rows=5, ready_for_import_rows=40),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-006",
        )
        assert report.reconciliation_readiness.is_ready is False
        assert report.reconciliation_readiness.reason == "blocked_rows_present"

    def test_reconciliation_readiness_fully_ready(self):
        inp = _make_input(needs_review_rows=0, ready_for_import_rows=50)
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=inp,
            adapter_source=_make_source(),
            batch_public_id="batch-prod-007",
        )
        assert report.reconciliation_readiness.is_ready is True
        assert report.reconciliation_readiness.reason == "ready"

    def test_reconciliation_readiness_no_ready_rows(self):
        inp = _make_input(ready_for_import_rows=0, parsed_rows=0, accepted_rows=0, imported_rows=0)
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=inp,
            adapter_source=_make_source(),
            batch_public_id="batch-prod-008",
        )
        assert report.reconciliation_readiness.is_ready is False
        assert report.reconciliation_readiness.reason == "no_ready_rows"

    def test_human_review_needed_with_blocked(self):
        inp = _make_input(blocked_rows=2, needs_review_rows=0)
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=inp,
            adapter_source=_make_source(),
            batch_public_id="batch-prod-009",
        )
        assert report.human_review_needed is True

    def test_human_review_not_needed_when_clean(self):
        inp = _make_input(blocked_rows=0, needs_review_rows=0)
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=inp,
            adapter_source=_make_source(),
            batch_public_id="batch-prod-010",
        )
        assert report.human_review_needed is False

    def test_issues_breakdown(self):
        issues = _make_issues(
            blocked_reason_counts=(("invalid_date", 2),),
            parser_warning_counts=(("parse_warn_x", 1),),
            total_blocks=2,
            total_warnings=1,
        )
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-011",
            issues=issues,
        )
        assert report.issues.total_blocks == 2
        assert report.issues.total_warnings == 1

    def test_issues_default_empty(self):
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-012",
        )
        assert report.issues.total_blocks == 0
        assert report.issues.total_warnings == 0
        assert report.issues.blocked_reason_counts == ()

    def test_parse_warnings_forwarded(self):
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-013",
            parse_warnings=("line 3: malformed date",),
        )
        assert report.parse_warnings == ("line 3: malformed date",)

    def test_extraction_warnings_forwarded(self):
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-014",
            extraction_warnings=("empty page 7",),
        )
        assert report.extraction_warnings == ("empty page 7",)

    def test_created_at_iso_forwarded(self):
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-015",
            created_at_iso="2026-07-09T12:00:00+00:00",
        )
        assert report.created_at_iso == "2026-07-09T12:00:00+00:00"

    def test_created_at_iso_default_none(self):
        report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-016",
        )
        assert report.created_at_iso is None

    def test_determinism(self):
        a = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-017",
        )
        b = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="batch-prod-017",
        )
        assert a == b
        assert a.source_evidence == b.source_evidence
        assert a.reconciliation_readiness == b.reconciliation_readiness


# ---------------------------------------------------------------------------
# Builder: production bridge result
# ---------------------------------------------------------------------------


class TestBuildFromProductionBridgeResult:
    def test_builds_report(self):
        report = build_pdf_import_run_report_from_production_bridge_result(
            adapter_input=_make_input(imported_rows=30),
            adapter_source=_make_source(),
            batch_public_id="bridge-prod-001",
            matched_count=40,
            review_required_count=10,
            blocked_count=2,
            ready_for_import_count=38,
        )
        assert isinstance(report, PdfImportRunReport)
        assert report.batch_public_id == "bridge-prod-001"

    def test_bridge_counts_replace_import_counts(self):
        report = build_pdf_import_run_report_from_production_bridge_result(
            adapter_input=_make_input(imported_rows=30, blocked_rows=1),
            adapter_source=_make_source(),
            batch_public_id="bridge-prod-002",
            matched_count=40,
            review_required_count=10,
            blocked_count=2,
            ready_for_import_count=38,
        )
        assert report.outcomes.imported_rows == 40
        assert report.outcomes.blocked_rows == 2
        assert report.outcomes.ready_for_import_rows == 38

    def test_needs_review_derived(self):
        report = build_pdf_import_run_report_from_production_bridge_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="bridge-prod-003",
            matched_count=35,
            review_required_count=15,
            blocked_count=5,
            ready_for_import_count=35,
        )
        assert report.outcomes.needs_review_rows == 10

    def test_needs_review_never_negative(self):
        report = build_pdf_import_run_report_from_production_bridge_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="bridge-prod-004",
            matched_count=50,
            review_required_count=2,
            blocked_count=5,
            ready_for_import_count=45,
        )
        assert report.outcomes.needs_review_rows == 0

    def test_guard_flags(self):
        report = build_pdf_import_run_report_from_production_bridge_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="bridge-prod-005",
            matched_count=40,
            review_required_count=10,
            blocked_count=3,
            ready_for_import_count=37,
        )
        assert report.review_only is True
        assert report.not_final_financial_record is True

    def test_source_evidence_preserved(self):
        report = build_pdf_import_run_report_from_production_bridge_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="bridge-prod-006",
            matched_count=40,
            review_required_count=10,
            blocked_count=3,
            ready_for_import_count=37,
        )
        assert report.source_evidence.source_pdf_path == "/data/statements/2025-01.pdf"

    def test_readiness_blocked_from_bridge(self):
        report = build_pdf_import_run_report_from_production_bridge_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="bridge-prod-008",
            matched_count=20,
            review_required_count=10,
            blocked_count=5,
            ready_for_import_count=20,
        )
        assert report.reconciliation_readiness.is_ready is False
        assert report.reconciliation_readiness.reason == "blocked_rows_present"


# ---------------------------------------------------------------------------
# Dashboard / audit payload separation
# ---------------------------------------------------------------------------


class TestDashboardAuditPayloadSeparation:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.report = build_pdf_import_run_report_from_production_import_result(
            adapter_input=_make_input(),
            adapter_source=_make_source(),
            batch_public_id="payload-test-001",
        )

    def test_dashboard_excludes_source_evidence(self):
        payload = export_pdf_import_run_report_dashboard_payload(self.report)
        se = payload["source_evidence"]
        assert "source_pdf_path" not in se
        assert "source_statement_id" not in se
        assert "attachment_path" not in se
        assert se.get("source_mode") == "pdf_text"

    def test_audit_includes_source_evidence(self):
        payload = export_pdf_import_run_report_audit_payload(self.report)
        se = payload["source_evidence"]
        assert se["source_pdf_path"] == "/data/statements/2025-01.pdf"
        assert se["source_statement_id"] == "stmt-prod-abc-001"
        assert se["attachment_path"] == "/data/attachments/abc-001.pdf"

    def test_both_json_serializable(self):
        dash = export_pdf_import_run_report_dashboard_payload(self.report)
        audit = export_pdf_import_run_report_audit_payload(self.report)
        assert json.dumps(dash) is not None
        assert json.dumps(audit) is not None

    def test_audit_is_superset(self):
        dash = export_pdf_import_run_report_dashboard_payload(self.report)
        audit = export_pdf_import_run_report_audit_payload(self.report)
        for key in dash:
            assert key in audit

    def test_text_output_includes_guard_flags(self):
        text = format_pdf_import_run_report_text(self.report)
        assert "review_only:          True" in text
        assert "not_final_financial_record: True" in text


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------


class TestConvenienceWrappers:
    @pytest.fixture(autouse=True)
    def _setup(self):
        from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
            DEFAULT_BATCH_PUBLIC_ID,
            DEFAULT_PDF_FIXTURE_PATH,
            DEFAULT_TEMPLATE_ID,
            DEFAULT_TEXT_FIXTURE_PATH,
            import_pdf_statement_fixture_to_temp_db,
        )

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = str(Path(self._tmpdir.name) / "adapter_test.db")
        self.import_result = import_pdf_statement_fixture_to_temp_db(
            db_path=db_path,
            pdf_path=str(DEFAULT_PDF_FIXTURE_PATH),
            text_fixture_path=str(DEFAULT_TEXT_FIXTURE_PATH),
            template_id=DEFAULT_TEMPLATE_ID,
            batch_public_id=DEFAULT_BATCH_PUBLIC_ID,
            source_mode="fixture_text",
        )

    def teardown_method(self):
        self._tmpdir.cleanup()

    def test_from_import_result_convenience(self):
        report = build_pdf_import_run_report_production_adapter_from_import_result(
            self.import_result,
        )
        assert isinstance(report, PdfImportRunReport)
        assert report.review_only is True
        assert report.not_final_financial_record is True

    def test_from_import_result_preserves_source(self):
        report = build_pdf_import_run_report_production_adapter_from_import_result(
            self.import_result,
        )
        assert report.source_evidence.source_pdf_path == str(self.import_result.pdf_path)

    def test_from_bridge_result_convenience(self):
        from finance_core.reconciliation.pdf_statement_review_queue_bridge import (
            run_pdf_statement_review_queue_bridge,
        )

        bridge = run_pdf_statement_review_queue_bridge(
            db_path=self.import_result.db_path,
            source_mode="fixture_text",
        )
        report = build_pdf_import_run_report_production_adapter_from_bridge_result(bridge)
        assert isinstance(report, PdfImportRunReport)
        assert report.review_only is True
        assert report.not_final_financial_record is True

    def test_from_bridge_result_counts_match(self):
        from finance_core.reconciliation.pdf_statement_review_queue_bridge import (
            run_pdf_statement_review_queue_bridge,
        )

        bridge = run_pdf_statement_review_queue_bridge(
            db_path=self.import_result.db_path,
            source_mode="fixture_text",
        )
        report = build_pdf_import_run_report_production_adapter_from_bridge_result(bridge)
        assert report.outcomes.blocked_rows == bridge.blocked_count
        assert report.outcomes.ready_for_import_rows == bridge.ready_for_import_count


# ---------------------------------------------------------------------------
# Safety: no DB writes, no live DB, no mutation imports
# ---------------------------------------------------------------------------


def _module_source_without_docstring(mod):
    """Return module source without the module-level docstring."""
    source = inspect.getsource(mod)
    # Strip the module-level triple-quoted docstring so safety contracts
    # that list non-goals in natural language do not trigger false positives.
    return re.sub(r'^"""[\s\S]*?"""', "", source, count=1)


class TestSafetyNoDBWritesOrMutations:
    def test_no_sqlite_write_imports(self):
        import finance_core.reconciliation.pdf_import_run_report_production_adapter as mod

        source = _module_source_without_docstring(mod)
        assert "sqlite3" not in source

    def test_no_live_db_reference(self):
        import finance_core.reconciliation.pdf_import_run_report_production_adapter as mod

        source = _module_source_without_docstring(mod)
        assert "database/finance.db" not in source
        assert "LIVE_DB_PATH" not in source

    def test_no_settlement_import(self):
        import finance_core.reconciliation.pdf_import_run_report_production_adapter as mod

        source = _module_source_without_docstring(mod)
        assert "settlement" not in source

    def test_no_final_mutation_import(self):
        import finance_core.reconciliation.pdf_import_run_report_production_adapter as mod

        source = _module_source_without_docstring(mod)
        assert "final_mutation" not in source.lower()

    def test_no_ai_or_model_import(self):
        import finance_core.reconciliation.pdf_import_run_report_production_adapter as mod

        source = _module_source_without_docstring(mod)
        assert "openai" not in source.lower()

    def test_no_telegram_import(self):
        import finance_core.reconciliation.pdf_import_run_report_production_adapter as mod

        source = _module_source_without_docstring(mod)
        assert "telegram" not in source.lower()

    def test_no_ocr_import(self):
        import finance_core.reconciliation.pdf_import_run_report_production_adapter as mod

        source = _module_source_without_docstring(mod)
        assert "ocr" not in source.lower()

    def test_no_metabase_import(self):
        import finance_core.reconciliation.pdf_import_run_report_production_adapter as mod

        source = _module_source_without_docstring(mod)
        assert "metabase" not in source.lower()


# ---------------------------------------------------------------------------
# Public API shape
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_all_exports(self):
        from finance_core.reconciliation.pdf_import_run_report_production_adapter import (
            __all__ as exports,
        )

        expected = {
            "PdfImportRunReportProductionAdapterInput",
            "PdfImportRunReportProductionAdapterSource",
            "build_pdf_import_run_report_from_production_import_result",
            "build_pdf_import_run_report_from_production_bridge_result",
            "build_pdf_import_run_report_production_adapter_from_import_result",
            "build_pdf_import_run_report_production_adapter_from_bridge_result",
        }
        assert set(exports) == expected
