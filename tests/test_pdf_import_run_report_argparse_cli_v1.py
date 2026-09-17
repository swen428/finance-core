"""Tests for pdf_import_run_report_cli argparse entry point v1."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest

from finance_core.reconciliation.pdf_import_run_report_cli import (
    build_arg_parser,
    main,
    run_pdf_import_run_report_fixture,
)

# ---------------------------------------------------------------------------
# Parser defaults
# ---------------------------------------------------------------------------


class TestParserDefaults:
    def test_output_default_is_text(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args([])
        assert args.output == "text"

    def test_source_mode_default_is_fixture_text(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args([])
        assert args.source_mode == "fixture_text"

    def test_db_path_default_is_none(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args([])
        assert args.db_path is None


# ---------------------------------------------------------------------------
# Parser choices
# ---------------------------------------------------------------------------


class TestParserChoices:
    def test_output_accepts_text(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--output", "text"])
        assert args.output == "text"

    def test_output_accepts_dashboard_json(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--output", "dashboard-json"])
        assert args.output == "dashboard-json"

    def test_output_accepts_audit_json(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--output", "audit-json"])
        assert args.output == "audit-json"

    def test_source_mode_accepts_fixture_text(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--source-mode", "fixture_text"])
        assert args.source_mode == "fixture_text"

    def test_source_mode_accepts_pdf_text(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(["--source-mode", "pdf_text"])
        assert args.source_mode == "pdf_text"


# ---------------------------------------------------------------------------
# Invalid choices rejected
# ---------------------------------------------------------------------------


class TestInvalidChoicesRejected:
    def test_invalid_output_rejected(self) -> None:
        parser = build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--output", "csv"])

    def test_invalid_source_mode_rejected(self) -> None:
        parser = build_arg_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--source-mode", "unknown"])


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------


class TestMainIntegration:
    def test_main_text_returns_zero(self) -> None:
        exit_code = main(["--output", "text"])
        assert exit_code == 0

    def test_main_dashboard_json_returns_zero(self) -> None:
        exit_code = main(["--output", "dashboard-json"])
        assert exit_code == 0

    def test_main_audit_json_returns_zero(self) -> None:
        exit_code = main(["--output", "audit-json"])
        assert exit_code == 0

    def test_main_defaults_return_zero(self) -> None:
        exit_code = main([])
        assert exit_code == 0


# ---------------------------------------------------------------------------
# --output text
# ---------------------------------------------------------------------------


class TestOutputText:
    def test_text_output_includes_run_report_header(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(["--output", "text"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "PDF Import Run Report" in captured.out

    def test_text_output_includes_guard_flags(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--output", "text"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "review_only" in captured.out.lower()
        assert "not_final_financial_record" in captured.out.lower()


# ---------------------------------------------------------------------------
# --output dashboard-json
# ---------------------------------------------------------------------------


class TestOutputDashboardJson:
    def test_dashboard_json_is_valid(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--output", "dashboard-json"])
        captured = capsys.readouterr()
        assert exit_code == 0
        payload = json.loads(captured.out)
        assert isinstance(payload, dict)

    def test_dashboard_json_excludes_audit_fields(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--output", "dashboard-json"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "source_pdf_path" not in captured.out
        assert "source_statement_id" not in captured.out
        assert "attachment_path" not in captured.out

    def test_dashboard_json_source_evidence_null(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--output", "dashboard-json"])
        captured = capsys.readouterr()
        assert exit_code == 0
        payload = json.loads(captured.out)
        se = payload.get("source_evidence", {})
        assert se.get("source_pdf_path") is None
        assert se.get("attachment_path") is None


# ---------------------------------------------------------------------------
# --output audit-json
# ---------------------------------------------------------------------------


class TestOutputAuditJson:
    def test_audit_json_is_valid(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--output", "audit-json"])
        captured = capsys.readouterr()
        assert exit_code == 0
        payload = json.loads(captured.out)
        assert isinstance(payload, dict)

    def test_audit_json_includes_source_evidence(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--output", "audit-json"])
        captured = capsys.readouterr()
        assert exit_code == 0
        payload = json.loads(captured.out)
        se = payload.get("source_evidence", {})
        assert "source_pdf_path" in se
        assert "source_statement_id" in se
        assert "attachment_path" in se

    def test_audit_json_contains_outcomes(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--output", "audit-json"])
        captured = capsys.readouterr()
        assert exit_code == 0
        payload = json.loads(captured.out)
        outcomes = payload.get("outcomes", {})
        assert "parsed_rows" in outcomes
        assert "accepted_rows" in outcomes
        assert "imported_rows" in outcomes


# ---------------------------------------------------------------------------
# --db-path
# ---------------------------------------------------------------------------


class TestDbPath:
    def test_db_path_uses_caller_provided_path(self, capsys: pytest.CaptureFixture[str]) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            exit_code = main(["--db-path", db_path, "--output", "text"])
            captured = capsys.readouterr()
            assert exit_code == 0
            assert "PDF Import Run Report" in captured.out
            # DB file should exist and have content (SQLite created tables)
            assert Path(db_path).exists()
            assert Path(db_path).stat().st_size > 0
        finally:
            Path(db_path).unlink(missing_ok=True)

    def test_db_path_does_not_touch_live_db(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--db-path with a temp file must not touch database/finance.db."""
        live_db = Path("database/finance.db")
        live_exists_before = live_db.exists()
        live_size_before = live_db.stat().st_size if live_exists_before else None

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            exit_code = main(["--db-path", db_path, "--output", "text"])
            assert exit_code == 0

            if live_exists_before:
                live_size_after = live_db.stat().st_size
                assert live_size_after == live_size_before, (
                    "database/finance.db should not be modified"
                )
        finally:
            Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Safety source inspection
# ---------------------------------------------------------------------------


class TestSafetySourceInspection:
    def test_no_live_db_reference_in_module(self) -> None:
        """Argparse module must not reference database/finance.db."""
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
        # Strip triple-quoted strings (module docstring and help text) so that
        # safety-boundary documentation does not cause false positives.
        source = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        assert "database/finance.db" not in source

    def test_no_settlement_imports(self) -> None:
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
        module_path = (
            Path(__file__).resolve().parents[1]
            / "finance_core"
            / "reconciliation"
            / "pdf_import_run_report_cli.py"
        )
        source = module_path.read_text(encoding="utf-8")
        assert "final_mutation" not in source

    def test_no_telegram_ocr_metabase_imports(self) -> None:
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
    def test_run_fixture_still_works(self) -> None:
        """Existing run_pdf_import_run_report_fixture API must still work."""
        result = run_pdf_import_run_report_fixture(output="text")
        assert result.report is not None
        assert result.output_mode == "text"
        assert len(result.output_text) > 0

    def test_main_uses_fixture_not_production_flow(self) -> None:
        """main() must delegate to the fixture function, not production wiring."""
        module_path = (
            Path(__file__).resolve().parents[1]
            / "finance_core"
            / "reconciliation"
            / "pdf_import_run_report_cli.py"
        )
        source = module_path.read_text(encoding="utf-8")
        # Strip triple-quoted strings so docstring non-goals don't trigger.
        source = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        assert "run_pdf_import_run_report_fixture" in source
        # Assert production/final imports are absent.
        assert "apply_persistence" not in source
        assert "settlement" not in source.lower()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_build_arg_parser_is_importable(self) -> None:
        assert callable(build_arg_parser)

    def test_main_is_importable(self) -> None:
        assert callable(main)

    def test_module_exports(self) -> None:
        from finance_core.reconciliation import pdf_import_run_report_cli

        assert hasattr(pdf_import_run_report_cli, "build_arg_parser")
        assert hasattr(pdf_import_run_report_cli, "main")
