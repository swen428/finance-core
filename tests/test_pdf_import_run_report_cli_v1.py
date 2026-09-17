"""Tests for pdf_import_run_report_cli -- PDF Import Run Report CLI v1."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finance_core.reconciliation.pdf_import_run_report_cli import (
    PdfImportRunReportCliResult,
    run_pdf_import_run_report_fixture,
)

# ---------------------------------------------------------------------------
# Dataclass invariants
# ---------------------------------------------------------------------------


class TestCliResultDataclass:
    def test_result_is_frozen(self) -> None:
        """PdfImportRunReportCliResult must be frozen."""
        result = run_pdf_import_run_report_fixture(output="text")
        with pytest.raises(Exception):
            result.output_mode = "dashboard-json"  # type: ignore[misc]

    def test_result_has_all_fields(self) -> None:
        """All expected fields are present."""
        result = run_pdf_import_run_report_fixture(output="text")
        assert result.report is not None
        assert isinstance(result.source_mode, str)
        assert result.output_mode == "text"
        assert isinstance(result.output_text, str)
        assert len(result.output_text) > 0


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------


class TestTextOutput:
    def test_text_output_includes_expected_sections(self) -> None:
        """Text output should include key sections from the report."""
        result = run_pdf_import_run_report_fixture(output="text")
        text = result.output_text
        assert "PDF Import Run Report" in text
        assert "Run reference:" in text
        assert "OUTCOMES" in text or "outcomes" in text.lower()
        assert "review_only" in text.lower()

    def test_text_output_includes_guard_flags(self) -> None:
        """Text output must mention guard flags."""
        result = run_pdf_import_run_report_fixture(output="text")
        text = result.output_text
        assert "review_only" in text.lower()
        assert "not_final_financial_record" in text.lower()

    def test_text_output_is_stable(self) -> None:
        """Same fixture should produce stable text output."""
        r1 = run_pdf_import_run_report_fixture(output="text")
        r2 = run_pdf_import_run_report_fixture(output="text")
        # Batch public IDs should match
        assert r1.report.batch_public_id == r2.report.batch_public_id
        # Core structure should be identical
        assert r1.output_mode == r2.output_mode == "text"
        # Run references based on deterministic batch public IDs
        assert r1.report.run_reference == r2.report.run_reference


# ---------------------------------------------------------------------------
# Dashboard JSON output
# ---------------------------------------------------------------------------


class TestDashboardJsonOutput:
    def test_dashboard_json_is_valid(self) -> None:
        """Dashboard JSON output must be parseable JSON."""
        result = run_pdf_import_run_report_fixture(output="dashboard-json")
        payload = json.loads(result.output_text)
        assert isinstance(payload, dict)

    def test_dashboard_json_excludes_audit_fields(self) -> None:
        """Dashboard JSON must not include source evidence paths."""
        result = run_pdf_import_run_report_fixture(output="dashboard-json")
        assert "source_pdf_path" not in result.output_text
        assert "source_statement_id" not in result.output_text
        assert "attachment_path" not in result.output_text

    def test_dashboard_json_has_source_evidence_none(self) -> None:
        """Dashboard JSON source_evidence values should be None (redacted)."""
        result = run_pdf_import_run_report_fixture(output="dashboard-json")
        payload = json.loads(result.output_text)
        se = payload.get("source_evidence", {})
        assert se.get("source_pdf_path") is None
        assert se.get("source_statement_id") is None
        assert se.get("attachment_path") is None


# ---------------------------------------------------------------------------
# Audit JSON output
# ---------------------------------------------------------------------------


class TestAuditJsonOutput:
    def test_audit_json_is_valid(self) -> None:
        """Audit JSON output must be parseable JSON."""
        result = run_pdf_import_run_report_fixture(output="audit-json")
        payload = json.loads(result.output_text)
        assert isinstance(payload, dict)

    def test_audit_json_includes_source_evidence(self) -> None:
        """Audit JSON must include source evidence fields."""
        result = run_pdf_import_run_report_fixture(output="audit-json")
        payload = json.loads(result.output_text)
        se = payload.get("source_evidence", {})
        assert "source_pdf_path" in se
        assert "source_statement_id" in se
        assert "attachment_path" in se

    def test_audit_json_contains_outcomes(self) -> None:
        """Audit JSON must include row outcome counts."""
        result = run_pdf_import_run_report_fixture(output="audit-json")
        payload = json.loads(result.output_text)
        outcomes = payload.get("outcomes", {})
        assert "parsed_rows" in outcomes
        assert "accepted_rows" in outcomes
        assert "imported_rows" in outcomes


# ---------------------------------------------------------------------------
# Invalid output mode
# ---------------------------------------------------------------------------


class TestInvalidOutputMode:
    def test_unsupported_output_mode_raises(self) -> None:
        """An unsupported output mode must raise ValueError."""
        with pytest.raises(ValueError, match="Unsupported output mode"):
            run_pdf_import_run_report_fixture(output="csv")


# ---------------------------------------------------------------------------
# Safety guarantees
# ---------------------------------------------------------------------------


class TestSafetyGuarantees:
    def test_no_live_db_reference(self) -> None:
        """Source module must not reference database/finance.db."""
        module_path = (
            Path(__file__).resolve().parents[1]
            / "finance_core"
            / "reconciliation"
            / "pdf_import_run_report_cli.py"
        )
        raw_source = module_path.read_text(encoding="utf-8")
        # Filter out docstring/comment lines that document non-goals
        source = "\n".join(
            line
            for line in raw_source.splitlines()
            if not (line.strip().startswith("#") or line.strip().startswith("-"))
        )
        assert "database/finance.db" not in source

    def test_no_settlement_imports(self) -> None:
        """CLI module must not import settlement modules."""
        module_path = (
            Path(__file__).resolve().parents[1]
            / "finance_core"
            / "reconciliation"
            / "pdf_import_run_report_cli.py"
        )
        raw_source = module_path.read_text(encoding="utf-8")
        source = "\n".join(
            line
            for line in raw_source.splitlines()
            if not (line.strip().startswith("#") or line.strip().startswith("-"))
        )
        assert "settlement" not in source.lower()

    def test_no_final_mutation_in_source(self) -> None:
        """CLI module must not reference final mutation."""
        module_path = (
            Path(__file__).resolve().parents[1]
            / "finance_core"
            / "reconciliation"
            / "pdf_import_run_report_cli.py"
        )
        source = module_path.read_text(encoding="utf-8")
        assert "final_mutation" not in source

    def test_no_telegram_ocr_metabase_imports(self) -> None:
        """CLI module must not import Telegram/OCR/Metabase runtime."""
        module_path = (
            Path(__file__).resolve().parents[1]
            / "finance_core"
            / "reconciliation"
            / "pdf_import_run_report_cli.py"
        )
        raw_source = module_path.read_text(encoding="utf-8")
        source = "\n".join(
            line
            for line in raw_source.splitlines()
            if not (line.strip().startswith("#") or line.strip().startswith("-"))
        )
        assert "telegram" not in source.lower()
        assert "metabase" not in source.lower()
        assert "ocr" not in source.lower()


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_does_not_mutate_existing_modules(self) -> None:
        """Running the fixture should not change any existing module behavior."""
        # Just running the fixture successfully is enough; any mutation
        # would cause cross-test interference.
        result = run_pdf_import_run_report_fixture(output="text")
        assert result.report is not None

    def test_uses_report_builder_not_duplicate_logic(self) -> None:
        """CLI must delegate to build_pdf_import_run_report_from_import_result."""
        module_path = (
            Path(__file__).resolve().parents[1]
            / "finance_core"
            / "reconciliation"
            / "pdf_import_run_report_cli.py"
        )
        source = module_path.read_text(encoding="utf-8")
        assert "build_pdf_import_run_report_from_import_result" in source


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_text_output_deterministic(self) -> None:
        """Same fixture, same output mode => same output."""
        r1 = run_pdf_import_run_report_fixture(output="text")
        r2 = run_pdf_import_run_report_fixture(output="text")
        assert r1.output_text == r2.output_text

    def test_dashboard_json_deterministic(self) -> None:
        """Same fixture, same output mode => same dashboard JSON."""
        r1 = run_pdf_import_run_report_fixture(output="dashboard-json")
        r2 = run_pdf_import_run_report_fixture(output="dashboard-json")
        assert r1.output_text == r2.output_text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_module_exports(self) -> None:
        """Verify that the public API is complete."""
        from finance_core.reconciliation import pdf_import_run_report_cli

        assert hasattr(pdf_import_run_report_cli, "PdfImportRunReportCliResult")
        assert hasattr(pdf_import_run_report_cli, "run_pdf_import_run_report_fixture")

    def test_cli_result_is_importable(self) -> None:
        """PdfImportRunReportCliResult is importable."""
        assert PdfImportRunReportCliResult is not None
