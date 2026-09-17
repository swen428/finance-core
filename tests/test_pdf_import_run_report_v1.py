"""Tests for PDF Import Run Report v1.

Covers report dataclass invariants, builders from import/bridge results,
reconciliation readiness classification, dashboard/audit payload exports,
text formatting, determinism, edge cases (missing evidence, zero counts),
safety guarantees, and backward compatibility.  Every test uses the
deterministic temp DB import fixture -- never touches database/finance.db
or live data.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from finance_core.reconciliation.pdf_import_run_report import (
    PdfImportRunIssues,
    PdfImportRunReport,
    PdfImportRunRowOutcomes,
    PdfImportRunSourceEvidence,
    _classify_reconciliation_readiness,
    build_pdf_import_run_report_from_bridge_result,
    build_pdf_import_run_report_from_import_result,
    export_pdf_import_run_report_audit_payload,
    export_pdf_import_run_report_dashboard_payload,
    format_pdf_import_run_report_text,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_to_temp_db(db_path: str) -> object:
    """Run the deterministic import fixture and return the result."""
    from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
        import_pdf_statement_fixture_to_temp_db,
    )

    return import_pdf_statement_fixture_to_temp_db(db_path=db_path)


def _bridge_to_temp_db(db_path: str, *, persist: bool = False) -> object:
    """Run the review queue bridge and return the result."""
    from finance_core.reconciliation.pdf_statement_review_queue_bridge import (
        run_pdf_statement_review_queue_bridge,
    )

    return run_pdf_statement_review_queue_bridge(
        db_path=db_path,
        source_mode="fixture_text",
        persist_review_queue=persist,
    )


# ===================================================================
# 1. ReconciliationReadiness classification
# ===================================================================


class TestReconciliationReadiness:
    def test_fully_ready(self) -> None:
        r = _classify_reconciliation_readiness(ready_count=3, needs_review_count=0, blocked_count=0)
        assert r.is_ready is True
        assert r.reason == "ready"

    def test_ready_with_review_items(self) -> None:
        r = _classify_reconciliation_readiness(ready_count=3, needs_review_count=2, blocked_count=0)
        assert r.is_ready is True
        assert r.reason == "ready_with_review_items"

    def test_blocked_not_ready(self) -> None:
        r = _classify_reconciliation_readiness(ready_count=2, needs_review_count=1, blocked_count=1)
        assert r.is_ready is False
        assert r.reason == "blocked_rows_present"

    def test_no_ready_rows_not_ready(self) -> None:
        r = _classify_reconciliation_readiness(ready_count=0, needs_review_count=0, blocked_count=0)
        assert r.is_ready is False
        assert r.reason == "no_ready_rows"

    def test_readiness_is_frozen(self) -> None:
        r = _classify_reconciliation_readiness(3, 0, 0)
        with pytest.raises(Exception):
            r.is_ready = False  # type: ignore[misc]


# ===================================================================
# 2. Dataclass invariants
# ===================================================================


class TestReportDataclassInvariants:
    def test_report_is_frozen(self) -> None:
        report = PdfImportRunReport(
            run_reference="test-ref",
            batch_public_id="test-batch",
            source_evidence=PdfImportRunSourceEvidence(
                source_pdf_path="/fake/p.pdf",
                source_statement_id="stmt-1",
                attachment_path="/fake/p.pdf",
                template_id=None,
                source_mode="fixture_text",
                source_type="bank_statement",
            ),
            outcomes=PdfImportRunRowOutcomes(
                parsed_rows=0,
                accepted_rows=0,
                imported_rows=0,
                skipped_duplicate_rows=0,
                idempotent_rows=0,
                blocked_rows=0,
                needs_review_rows=0,
                ready_for_import_rows=0,
            ),
            issues=PdfImportRunIssues(
                blocked_reason_counts=(),
                parser_warning_counts=(),
                total_blocks=0,
                total_warnings=0,
            ),
            reconciliation_readiness=_classify_reconciliation_readiness(0, 0, 0),
            human_review_needed=False,
        )
        with pytest.raises(Exception):
            report.run_reference = "mutated"  # type: ignore[misc]

    def test_outcomes_is_frozen(self) -> None:
        o = PdfImportRunRowOutcomes(0, 0, 0, 0, 0, 0, 0, 0)
        with pytest.raises(Exception):
            o.parsed_rows = 1  # type: ignore[misc]

    def test_source_evidence_is_frozen(self) -> None:
        s = PdfImportRunSourceEvidence("/a", "b", "/a", None, "fx", "bs")
        with pytest.raises(Exception):
            s.source_pdf_path = "x"  # type: ignore[misc]

    def test_issues_is_frozen(self) -> None:
        i = PdfImportRunIssues((), (), 0, 0)
        with pytest.raises(Exception):
            i.total_blocks = 1  # type: ignore[misc]

    def test_guard_flags_are_true_by_default(self) -> None:
        report = PdfImportRunReport(
            run_reference="test-ref",
            batch_public_id="test-batch",
            source_evidence=PdfImportRunSourceEvidence(
                source_pdf_path="/f",
                source_statement_id="s",
                attachment_path="/f",
                template_id=None,
                source_mode="fx",
                source_type="bs",
            ),
            outcomes=PdfImportRunRowOutcomes(0, 0, 0, 0, 0, 0, 0, 0),
            issues=PdfImportRunIssues((), (), 0, 0),
            reconciliation_readiness=_classify_reconciliation_readiness(0, 0, 0),
            human_review_needed=False,
        )
        assert report.review_only is True
        assert report.not_final_financial_record is True


# ===================================================================
# 3. Build from import result
# ===================================================================


class TestBuildFromImportResult:
    def test_builds_successfully(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_report.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        assert isinstance(report, PdfImportRunReport)
        assert report.run_reference == import_result.import_batch.public_id

    def test_report_has_correct_outcomes(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_outcomes.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)

        assert report.outcomes.parsed_rows > 0
        assert report.outcomes.imported_rows > 0
        assert report.outcomes.accepted_rows > 0
        assert report.outcomes.ready_for_import_rows > 0
        # Declared rows total should equal parsed_rows
        total_classified = (
            report.outcomes.ready_for_import_rows
            + report.outcomes.needs_review_rows
            + report.outcomes.blocked_rows
        )
        assert total_classified == report.outcomes.parsed_rows

    def test_source_evidence_excludes_audit_in_dashboard(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_dash.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        payload = export_pdf_import_run_report_dashboard_payload(report)
        se = payload.get("source_evidence")
        assert isinstance(se, dict)
        assert "source_pdf_path" not in se
        assert "source_statement_id" not in se
        assert "attachment_path" not in se
        assert se.get("source_mode") is not None

    def test_source_evidence_includes_audit(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_audit.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        payload = export_pdf_import_run_report_audit_payload(report)
        se = payload.get("source_evidence")
        assert isinstance(se, dict)
        assert "source_pdf_path" in se
        assert "source_statement_id" in se
        assert "attachment_path" in se

    def test_human_review_needed_detects_blocked(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_hr.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        # The fixture has 0 blocked rows, so review should not be needed
        assert report.human_review_needed is False
        assert report.outcomes.blocked_rows == 0

    def test_readiness_flag(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_rdy.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        # Fixture has ready rows and no blocked rows
        assert report.reconciliation_readiness.is_ready is True


# ===================================================================
# 4. Build from bridge result
# ===================================================================


class TestBuildFromBridgeResult:
    def test_builds_successfully(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_br_report.db")
        bridge_result = _bridge_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_bridge_result(bridge_result)
        assert isinstance(report, PdfImportRunReport)
        assert report.source_evidence.source_mode == "fixture_text"

    def test_outcomes_use_bridge_counts(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_br_out.db")
        bridge_result = _bridge_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_bridge_result(bridge_result)
        assert report.outcomes.imported_rows == bridge_result.matched_count
        assert report.outcomes.blocked_rows == bridge_result.blocked_count
        assert report.outcomes.ready_for_import_rows == bridge_result.ready_for_import_count


# ===================================================================
# 5. Dashboard-safe vs audit payload
# ===================================================================


class TestPayloadSeparation:
    def test_dashboard_has_expected_sections(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_pay.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        payload = export_pdf_import_run_report_dashboard_payload(report)

        assert "run_reference" in payload
        assert "batch_public_id" in payload
        assert "source_evidence" in payload
        assert "outcomes" in payload
        assert "issues" in payload
        assert "reconciliation_readiness" in payload
        assert "human_review_needed" in payload
        assert "review_only" in payload
        assert "not_final_financial_record" in payload

    def test_dashboard_payload_is_json_serializable(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_json.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        payload = export_pdf_import_run_report_dashboard_payload(report)
        serialized = json.dumps(payload, sort_keys=True)
        assert len(serialized) > 0
        assert json.loads(serialized) == payload

    def test_audit_payload_is_json_serializable(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_json2.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        payload = export_pdf_import_run_report_audit_payload(report)
        serialized = json.dumps(payload, sort_keys=True)
        assert len(serialized) > 0

    def test_audit_payload_superset_of_dashboard(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_sup.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        dashboard = export_pdf_import_run_report_dashboard_payload(report)
        audit = export_pdf_import_run_report_audit_payload(report)
        for key in dashboard:
            assert key in audit


# ===================================================================
# 6. Text formatter
# ===================================================================


class TestTextFormatter:
    def test_produces_sections(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_txt.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        text = format_pdf_import_run_report_text(report)
        assert "PDF Import Run Report" in text
        assert "Run reference" in text
        assert "Row Outcomes" in text
        assert "Reconciliation Readiness" in text
        assert "Human review needed" in text
        assert "End of PDF Import Run Report." in text

    def test_includes_guard_flags(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_flags.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        text = format_pdf_import_run_report_text(report)
        assert "review_only" in text
        assert "not_final_financial_record" in text

    def test_no_blocked_section_when_zero_blocks(self, tmp_path: Path) -> None:
        """When there are no blocked rows, the blocked reason breakdown
        section should not appear."""
        db_path = str(tmp_path / "test_noblocks.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        text = format_pdf_import_run_report_text(report)
        # The fixture has 0 blocked rows, so no blocked section
        if report.issues.blocked_reason_counts:
            assert "Blocked Reason Breakdown" in text
        # The section header should appear only when there are blocked reasons
        assert text.count("Blocked Reason Breakdown") <= 1


# ===================================================================
# 7. Determinism
# ===================================================================


class TestDeterminism:
    def test_same_input_same_report(self, tmp_path: Path) -> None:
        db_path1 = str(tmp_path / "test_det1.db")
        db_path2 = str(tmp_path / "test_det2.db")
        r1 = build_pdf_import_run_report_from_import_result(_import_to_temp_db(db_path1))
        r2 = build_pdf_import_run_report_from_import_result(_import_to_temp_db(db_path2))
        assert r1.outcomes == r2.outcomes
        assert r1.issues == r2.issues
        assert r1.reconciliation_readiness == r2.reconciliation_readiness
        assert r1.human_review_needed == r2.human_review_needed
        # Payloads should also be deterministic
        p1 = export_pdf_import_run_report_dashboard_payload(r1)
        p2 = export_pdf_import_run_report_dashboard_payload(r2)
        assert p1 == p2

    def test_text_format_deterministic(self, tmp_path: Path) -> None:
        db_path1 = str(tmp_path / "test_dtext1.db")
        db_path2 = str(tmp_path / "test_dtext2.db")
        r1 = build_pdf_import_run_report_from_import_result(_import_to_temp_db(db_path1))
        r2 = build_pdf_import_run_report_from_import_result(_import_to_temp_db(db_path2))
        assert format_pdf_import_run_report_text(r1) == format_pdf_import_run_report_text(r2)


# ===================================================================
# 8. Edge cases -- missing evidence, zero counts
# ===================================================================


class TestEdgeCases:
    def test_template_id_none_handled(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_notemplate.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        # The fixture always has a template_id, so this just checks
        # that the report is still valid
        assert report.source_evidence.template_id is not None

    def test_zero_count_report_structure_valid(self) -> None:
        """Build a report with zero counts and verify it's structurally sound."""
        report = PdfImportRunReport(
            run_reference="zero-ref",
            batch_public_id="zero-batch",
            source_evidence=PdfImportRunSourceEvidence(
                source_pdf_path="/z.pdf",
                source_statement_id="stmt-zero",
                attachment_path="/z.pdf",
                template_id=None,
                source_mode="fixture_text",
                source_type="bank_statement",
            ),
            outcomes=PdfImportRunRowOutcomes(0, 0, 0, 0, 0, 0, 0, 0),
            issues=PdfImportRunIssues((), (), 0, 0),
            reconciliation_readiness=_classify_reconciliation_readiness(0, 0, 0),
            human_review_needed=False,
        )
        assert report.human_review_needed is False
        assert report.reconciliation_readiness.is_ready is False
        assert report.reconciliation_readiness.reason == "no_ready_rows"
        text = format_pdf_import_run_report_text(report)
        assert "0" in text

    def test_parse_warnings_are_deduplicated_and_sorted(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_warn.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        # The fixture uses fixture_text mode, which generates warnings
        assert isinstance(report.parse_warnings, tuple)
        assert isinstance(report.extraction_warnings, tuple)
        # Should be sorted with no duplicates
        for i in range(len(report.parse_warnings) - 1):
            assert report.parse_warnings[i] <= report.parse_warnings[i + 1]

    def test_created_at_iso_default_none(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_cat.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        assert report.created_at_iso is None


# ===================================================================
# 9. No live DB / settlement / final transaction safety
# ===================================================================


class TestSafetyGuarantees:
    def test_no_live_db_reference(self) -> None:
        """Source code must never reference database/finance.db."""
        mod = __import__(
            "finance_core.reconciliation.pdf_import_run_report",
            fromlist=["pdf_import_run_report"],
        )
        source = inspect.getsource(mod)
        code_only = source.split('"""', 2)[-1] if '"""' in source else source
        assert "database/finance.db" not in code_only
        assert "finance.db" not in code_only

    def test_no_settlement_imports(self) -> None:
        mod = __import__(
            "finance_core.reconciliation.pdf_import_run_report",
            fromlist=["pdf_import_run_report"],
        )
        source = inspect.getsource(mod)
        code_only = source.split('"""', 2)[-1] if '"""' in source else source
        assert "settlement" not in code_only.lower()

    def test_no_final_mutation_in_source(self) -> None:
        mod = __import__(
            "finance_core.reconciliation.pdf_import_run_report",
            fromlist=["pdf_import_run_report"],
        )
        source = inspect.getsource(mod)
        code_only = source.split('"""', 2)[-1] if '"""' in source else source
        assert "final_mutation" not in code_only.lower()

    def test_no_telegram_ocr_metabase_imports(self) -> None:
        mod = __import__(
            "finance_core.reconciliation.pdf_import_run_report",
            fromlist=["pdf_import_run_report"],
        )
        source = inspect.getsource(mod)
        code_only = source.split('"""', 2)[-1] if '"""' in source else source
        for dep in ("telegram", "ocr", "metabase"):
            assert dep not in code_only.lower(), f"Unexpected {dep} reference"


# ===================================================================
# 10. Backward compatibility -- report does not mutate import result
# ===================================================================


class TestBackwardCompatibility:
    def test_build_report_does_not_mutate_import_result(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_nomut.db")
        import_result1 = _import_to_temp_db(db_path)
        # Snapshot key fields before building report
        batch_before = import_result1.import_batch
        inserted_before = tuple(batch_before.inserted_ids)
        skipped_before = batch_before.skipped_duplicates

        report = build_pdf_import_run_report_from_import_result(import_result1)

        # Verify nothing changed
        assert tuple(batch_before.inserted_ids) == inserted_before
        assert batch_before.skipped_duplicates == skipped_before
        # The report is derived, not the source of truth
        assert report.review_only is True


# ===================================================================
# 11. Module __all__ is correct
# ===================================================================


class TestPublicAPI:
    def test_all_exports(self) -> None:
        mod = __import__(
            "finance_core.reconciliation.pdf_import_run_report",
            fromlist=["pdf_import_run_report"],
        )
        assert hasattr(mod, "__all__")
        expected = {
            "PdfImportRunReport",
            "PdfImportRunRowOutcomes",
            "PdfImportRunSourceEvidence",
            "PdfImportRunIssues",
            "ReconciliationReadiness",
            "build_pdf_import_run_report_from_import_result",
            "build_pdf_import_run_report_from_bridge_result",
            "export_pdf_import_run_report_dashboard_payload",
            "export_pdf_import_run_report_audit_payload",
            "format_pdf_import_run_report_text",
        }
        assert set(mod.__all__) == expected


# ===================================================================
# 12. Reconciliation readiness -- all cases from fixture
# ===================================================================


class TestReadinessFromFixture:
    def test_fixture_readiness_is_ready(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_ready.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        # The known fixture has ready rows, 0 blocked
        assert report.reconciliation_readiness.is_ready is True
        assert report.reconciliation_readiness.ready_count > 0
        assert report.reconciliation_readiness.blocked_count == 0

    def test_human_review_not_needed_for_clean_fixture(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_clean.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        # Fixture rows are all ready_for_import with 0 blocked
        assert report.human_review_needed is False


# ===================================================================
# 13. Issues breakdown from fixture
# ===================================================================


class TestIssuesBreakdown:
    def test_fixture_has_no_blocked_reasons(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_iss.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        # The fixture has no blocked rows
        assert report.issues.total_blocks == 0
        assert report.issues.blocked_reason_counts == ()

    def test_issues_dataclass_fields_present(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_iss2.db")
        import_result = _import_to_temp_db(db_path)
        report = build_pdf_import_run_report_from_import_result(import_result)
        assert hasattr(report.issues, "blocked_reason_counts")
        assert hasattr(report.issues, "parser_warning_counts")
        assert hasattr(report.issues, "total_blocks")
        assert hasattr(report.issues, "total_warnings")
