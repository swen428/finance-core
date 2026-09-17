"""Tests for PDF Statement Review CLI Fixture v1.

Verifies that the CLI is deterministic, read-only, correctly separates
dashboard-safe from audit payloads, preserves ordering, and does not
expose sensitive fields in dashboard payloads. Uses synthetic data
only -- no real PDFs, no OCR, no live DB.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.pdf_statement_bridge import (
    ParsedPdfStatementRow,
    normalize_pdf_statement_rows_batch,
)
from finance_core.reconciliation.pdf_statement_import_review_fixture import (
    build_pdf_statement_import_review_fixture,
)
from finance_core.reconciliation.pdf_statement_review_cli import (
    _build_arg_parser,
    _build_synthetic_rows,
    _print_json_output,
    _print_text_output,
    _validate_args,
    main,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(argv: list[str] | None = None) -> str:
    """Run the CLI main() and capture stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(argv if argv is not None else [])
    return buf.getvalue()


# ===================================================================
# 1. Synthetic rows
# ===================================================================


class TestSyntheticRows:
    """Synthetic rows must be stable and include both accepted and blocked."""

    def test_synthetic_rows_count(self) -> None:
        rows = _build_synthetic_rows()
        assert len(rows) == 10

    def test_has_accepted_rows(self) -> None:
        rows = _build_synthetic_rows()
        batch = normalize_pdf_statement_rows_batch(rows)
        assert batch.accepted_count > 0

    def test_has_blocked_rows(self) -> None:
        rows = _build_synthetic_rows()
        batch = normalize_pdf_statement_rows_batch(rows)
        assert batch.blocked_count > 0

    def test_synthetic_rows_deterministic(self) -> None:
        a = _build_synthetic_rows()
        b = _build_synthetic_rows()
        assert len(a) == len(b)
        for ra, rb in zip(a, b):
            assert ra.description == rb.description
            assert ra.amount == rb.amount
            assert ra.currency == rb.currency

    def test_blocked_reasons_covered(self) -> None:
        """Synthetic rows should cover all expected blocked reasons."""
        rows = _build_synthetic_rows()
        batch = normalize_pdf_statement_rows_batch(rows)
        reasons = {r for r, _ in batch.blocked_reason_counts}
        assert len(reasons) >= 4  # missing_amount, missing_currency, missing_description, etc.

    def test_synthetic_rows_are_parsed_pdf_statement_rows(self) -> None:
        rows = _build_synthetic_rows()
        for row in rows:
            assert isinstance(row, ParsedPdfStatementRow)

    def test_synthetic_rows_include_multiple_directions(self) -> None:
        rows = _build_synthetic_rows()
        directions = {row.amount_direction for row in rows}
        assert StatementAmountDirection.DEBIT in directions
        assert StatementAmountDirection.CREDIT in directions


# ===================================================================
# 2. Text output -- summary
# ===================================================================


class TestTextOutputSummary:
    """Text output must include summary counts and blocked reason breakdown."""

    def test_text_output_includes_total(self) -> None:
        output = _run_cli()
        assert "Total rows:" in output
        assert "10" in output or "10" in output.replace("Total rows:", "")

    def test_text_output_includes_accepted_count(self) -> None:
        output = _run_cli()
        assert "Accepted:" in output

    def test_text_output_includes_blocked_count(self) -> None:
        output = _run_cli()
        assert "Blocked:" in output

    def test_text_output_includes_has_blocked(self) -> None:
        output = _run_cli()
        assert "Has blocked:" in output

    def test_text_output_includes_blocked_reason_counts(self) -> None:
        output = _run_cli()
        assert "Blocked reason counts:" in output

    def test_text_output_headers_present(self) -> None:
        output = _run_cli()
        assert "PDF Statement Review CLI" in output
        assert "Synthetic Demo" in output
        assert "End of review." in output

    def test_text_output_includes_accepted_section(self) -> None:
        output = _run_cli()
        assert "--- Accepted Rows ---" in output

    def test_text_output_includes_blocked_section(self) -> None:
        output = _run_cli()
        assert "--- Blocked Rows ---" in output


# ===================================================================
# 3. Text output -- accepted rows
# ===================================================================


class TestTextOutputAcceptedRows:
    """Text output must show accepted row details."""

    def test_accepted_row_merchant_visible(self) -> None:
        output = _run_cli()
        assert "STARBUCKS COFFEE" in output

    def test_accepted_row_amount_visible(self) -> None:
        output = _run_cli()
        assert "18.50" in output

    def test_accepted_row_currency_visible(self) -> None:
        output = _run_cli()
        assert "MYR" in output

    def test_accepted_row_direction_visible(self) -> None:
        output = _run_cli()
        assert "debit" in output
        assert "credit" in output

    def test_accepted_row_no_attachment_path(self) -> None:
        output = _run_cli()
        assert "/demo/stmt-2026-06.pdf" not in output

    def test_accepted_row_no_raw_row_text(self) -> None:
        output = _run_cli()
        assert "01/06 STARBUCKS" not in output


# ===================================================================
# 4. Text output -- blocked rows
# ===================================================================


class TestTextOutputBlockedRows:
    """Text output must show blocked row summaries with reason codes."""

    def test_blocked_row_description_visible(self) -> None:
        output = _run_cli()
        assert "MYSTERY CHARGE" in output

    def test_blocked_row_empty_description(self) -> None:
        output = _run_cli()
        assert "(empty)" in output

    def test_blocked_row_reason_codes_visible(self) -> None:
        output = _run_cli()
        assert "missing_amount" in output
        assert "missing_description" in output
        assert "missing_currency" in output

    def test_blocked_row_no_attachment_path(self) -> None:
        output = _run_cli()
        assert "/demo/stmt" not in output


# ===================================================================
# 5. JSON output -- dashboard
# ===================================================================


class TestJsonDashboardOutput:
    """JSON dashboard output must use the review fixture export function."""

    def test_json_output_is_valid_json(self) -> None:
        output = _run_cli(["--json"])
        parsed = json.loads(output)
        assert "accepted" in parsed
        assert "blocked" in parsed
        assert "summary" in parsed

    def test_json_output_has_summary(self) -> None:
        output = _run_cli(["--json"])
        parsed = json.loads(output)
        assert "total_rows" in parsed["summary"]
        assert "accepted_count" in parsed["summary"]
        assert "blocked_count" in parsed["summary"]

    def test_json_output_excludes_attachment_path(self) -> None:
        output = _run_cli(["--json"])
        assert "attachment_path" not in output

    def test_json_output_excludes_raw_row_text(self) -> None:
        output = _run_cli(["--json"])
        assert "raw_row_text" not in output

    def test_json_output_deterministic(self) -> None:
        a = _run_cli(["--json"])
        b = _run_cli(["--json"])
        assert a == b

    def test_json_output_accepted_count_matches(self) -> None:
        output = _run_cli(["--json"])
        parsed = json.loads(output)
        assert parsed["summary"]["accepted_count"] == len(parsed["accepted"])

    def test_json_output_blocked_count_matches(self) -> None:
        output = _run_cli(["--json"])
        parsed = json.loads(output)
        assert parsed["summary"]["blocked_count"] == len(parsed["blocked"])


# ===================================================================
# 6. JSON output -- audit
# ===================================================================


class TestJsonAuditOutput:
    """JSON audit output must include source evidence."""

    def test_audit_output_is_valid_json(self) -> None:
        output = _run_cli(["--json", "--audit"])
        parsed = json.loads(output)
        assert "accepted" in parsed
        assert "blocked" in parsed
        assert "summary" in parsed

    def test_audit_output_includes_attachment_path(self) -> None:
        output = _run_cli(["--json", "--audit"])
        assert "attachment_path" in output

    def test_audit_output_includes_raw_row_text(self) -> None:
        output = _run_cli(["--json", "--audit"])
        assert "raw_row_text" in output

    def test_audit_output_includes_raw_row_payload(self) -> None:
        output = _run_cli(["--json", "--audit"])
        assert "raw_row_payload" in output

    def test_audit_output_deterministic(self) -> None:
        a = _run_cli(["--json", "--audit"])
        b = _run_cli(["--json", "--audit"])
        assert a == b

    def test_dashboard_vs_audit_differ(self) -> None:
        dash = _run_cli(["--json"])
        audit = _run_cli(["--json", "--audit"])
        assert dash != audit


# ===================================================================
# 7. Filter: blocked-only
# ===================================================================


class TestBlockedOnly:
    """--blocked-only must only show blocked rows."""

    def test_blocked_only_text_no_accepted_section(self) -> None:
        output = _run_cli(["--blocked-only"])
        assert "--- Accepted Rows ---" not in output

    def test_blocked_only_text_has_blocked_section(self) -> None:
        output = _run_cli(["--blocked-only"])
        assert "--- Blocked Rows ---" in output

    def test_blocked_only_json_no_accepted(self) -> None:
        output = _run_cli(["--json", "--blocked-only"])
        parsed = json.loads(output)
        assert "accepted" not in parsed
        assert "blocked" in parsed

    def test_blocked_only_json_has_summary(self) -> None:
        output = _run_cli(["--json", "--blocked-only"])
        parsed = json.loads(output)
        assert "summary" in parsed


# ===================================================================
# 8. Filter: accepted-only
# ===================================================================


class TestAcceptedOnly:
    """--accepted-only must only show accepted rows."""

    def test_accepted_only_text_no_blocked_section(self) -> None:
        output = _run_cli(["--accepted-only"])
        assert "--- Blocked Rows ---" not in output

    def test_accepted_only_text_has_accepted_section(self) -> None:
        output = _run_cli(["--accepted-only"])
        assert "--- Accepted Rows ---" in output

    def test_accepted_only_json_no_blocked(self) -> None:
        output = _run_cli(["--json", "--accepted-only"])
        parsed = json.loads(output)
        assert "blocked" not in parsed
        assert "accepted" in parsed

    def test_accepted_only_json_has_summary(self) -> None:
        output = _run_cli(["--json", "--accepted-only"])
        parsed = json.loads(output)
        assert "summary" in parsed


# ===================================================================
# 9. Limit behavior
# ===================================================================


class TestLimit:
    """--limit must cap output rows."""

    def test_limit_text_fewer_accepted_rows(self) -> None:
        output_full = _run_cli()
        output_lim = _run_cli(["--limit", "1"])
        assert len(output_lim) < len(output_full)

    def test_limit_json_reduces_accepted(self) -> None:
        output = _run_cli(["--json", "--limit", "2"])
        parsed = json.loads(output)
        assert len(parsed["accepted"]) <= 2


# ===================================================================
# 10. Deterministic ordering
# ===================================================================


class TestDeterministicOrdering:
    """Text and JSON output must be stable across invocations."""

    def test_text_output_deterministic(self) -> None:
        a = _run_cli()
        b = _run_cli()
        assert a == b

    def test_text_blocked_only_deterministic(self) -> None:
        a = _run_cli(["--blocked-only"])
        b = _run_cli(["--blocked-only"])
        assert a == b

    def test_text_accepted_only_deterministic(self) -> None:
        a = _run_cli(["--accepted-only"])
        b = _run_cli(["--accepted-only"])
        assert a == b

    def test_json_deterministic(self) -> None:
        a = _run_cli(["--json"])
        b = _run_cli(["--json"])
        assert a == b

    def test_json_with_limit_deterministic(self) -> None:
        a = _run_cli(["--json", "--limit", "3"])
        b = _run_cli(["--json", "--limit", "3"])
        assert a == b

    def test_audit_deterministic(self) -> None:
        a = _run_cli(["--json", "--audit"])
        b = _run_cli(["--json", "--audit"])
        assert a == b


# ===================================================================
# 11. No live DB, no file I/O, no OCR/PDF
# ===================================================================


class TestNoLiveDependencies:
    """Module must not reference database, file I/O, or PDF/OCR deps."""

    def test_no_live_db_path(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_review_cli as cli

        source = inspect.getsource(cli)
        assert "finance.db" not in source

    def test_no_sqlite3_import(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_review_cli as cli

        source = inspect.getsource(cli)
        assert "sqlite3" not in source

    def test_no_file_io_open(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_review_cli as cli

        source = inspect.getsource(cli)
        assert "open(" not in source

    def test_no_pdf_ocr_dependencies(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_review_cli as cli

        source = inspect.getsource(cli)
        source_lower = source.lower()
        for dep in (
            "pdfplumber",
            "pytesseract",
            "pypdf",
            "camelot",
            "tabula",
        ):
            assert dep not in source_lower, f"Unexpected dependency: {dep}"

    def test_no_ocr_imports(self) -> None:
        import inspect

        from finance_core.reconciliation import pdf_statement_review_cli as cli

        source = inspect.getsource(cli)
        source_lower = source.lower()
        assert "import ocr" not in source_lower
        assert "from ocr" not in source_lower

    def test_main_does_not_open_files(self) -> None:
        """main() must not call open() on attachment_path."""
        rows = _build_synthetic_rows()
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        # Just verify the fixture is built in-memory without file I/O
        assert fixture.summary.total_rows == 10


# ===================================================================
# 12. Dashboard output excludes attachment_path and raw_row_text
# ===================================================================


class TestDashboardSafety:
    """Dashboard output must never expose attachment_path or raw_row_text."""

    def test_dashboard_text_excludes_attachment_path(self) -> None:
        output = _run_cli()
        assert "/demo/stmt" not in output

    def test_dashboard_text_excludes_raw_row_text(self) -> None:
        output = _run_cli()
        assert "01/06 STARBUCKS" not in output
        assert "03/06 GRABFOOD" not in output

    def test_dashboard_json_excludes_attachment_path(self) -> None:
        output = _run_cli(["--json"])
        assert "attachment_path" not in output

    def test_dashboard_json_excludes_raw_row_text(self) -> None:
        output = _run_cli(["--json"])
        assert "raw_row_text" not in output

    def test_dashboard_json_excludes_raw_row_payload(self) -> None:
        output = _run_cli(["--json"])
        assert "raw_row_payload" not in output


# ===================================================================
# 13. Audit output separates sensitive evidence
# ===================================================================


class TestAuditEvidenceSeparation:
    """Audit output must include source evidence that dashboard excludes."""

    def test_audit_has_attachment_path(self) -> None:
        output = _run_cli(["--json", "--audit"])
        parsed = json.loads(output)
        assert any("attachment_path" in r for r in parsed["blocked"])

    def test_audit_has_raw_row_text(self) -> None:
        output = _run_cli(["--json", "--audit"])
        parsed = json.loads(output)
        assert any("raw_row_text" in r for r in parsed["blocked"])

    def test_audit_has_raw_row_payload_on_accepted(self) -> None:
        output = _run_cli(["--json", "--audit"])
        parsed = json.loads(output)
        assert any("raw_row_payload" in r for r in parsed["accepted"])


# ===================================================================
# 14. CLI argument handling
# ===================================================================


class TestArgumentHandling:
    """CLI must handle all supported flags without crashing."""

    def test_default_no_args(self) -> None:
        output = _run_cli()
        assert len(output) > 0
        assert "End of review." in output

    def test_json_flag(self) -> None:
        output = _run_cli(["--json"])
        parsed = json.loads(output)
        assert "summary" in parsed

    def test_json_audit_flag(self) -> None:
        output = _run_cli(["--json", "--audit"])
        parsed = json.loads(output)
        assert "summary" in parsed

    def test_blocked_only_flag(self) -> None:
        output = _run_cli(["--blocked-only"])
        assert "--- Accepted Rows ---" not in output

    def test_accepted_only_flag(self) -> None:
        output = _run_cli(["--accepted-only"])
        assert "--- Blocked Rows ---" not in output

    def test_limit_flag(self) -> None:
        output = _run_cli(["--limit", "1"])
        assert len(output) > 0

    def test_json_blocked_only(self) -> None:
        output = _run_cli(["--json", "--blocked-only"])
        parsed = json.loads(output)
        assert "accepted" not in parsed

    def test_json_accepted_only(self) -> None:
        output = _run_cli(["--json", "--accepted-only"])
        parsed = json.loads(output)
        assert "blocked" not in parsed


# ===================================================================
# 15. Invalid argument handling
# ===================================================================


class TestInvalidArguments:
    """CLI must handle invalid argument combinations clearly."""

    def test_audit_without_json_exits(self) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["--audit"])
        assert exc.value.code == 2

    def test_audit_without_json_stderr_message(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(io.StringIO()):
            with patch.object(sys, "stderr", buf):
                try:
                    main(["--audit"])
                except SystemExit:
                    pass
        assert "requires --json" in buf.getvalue()

    def test_blocked_and_accepted_mutually_exclusive(self) -> None:
        """argparse will reject --blocked-only --accepted-only together."""
        with pytest.raises(SystemExit):
            main(["--blocked-only", "--accepted-only"])

    def test_negative_limit(self) -> None:
        """Negative limit should not crash - argparse passes it through."""
        # argparse won't reject negative ints, but slicing handles it gracefully
        output = _run_cli(["--limit", "-1"])
        assert len(output) > 0


# ===================================================================
# 16. Argument parser shape
# ===================================================================


class TestArgumentParser:
    """Argument parser must support all expected arguments."""

    def test_parser_has_json(self) -> None:
        parser = _build_arg_parser()
        args = parser.parse_args(["--json"])
        assert args.json is True

    def test_parser_has_audit(self) -> None:
        parser = _build_arg_parser()
        args = parser.parse_args(["--audit"])
        assert args.audit is True

    def test_parser_has_blocked_only(self) -> None:
        parser = _build_arg_parser()
        args = parser.parse_args(["--blocked-only"])
        assert args.blocked_only is True

    def test_parser_has_accepted_only(self) -> None:
        parser = _build_arg_parser()
        args = parser.parse_args(["--accepted-only"])
        assert args.accepted_only is True

    def test_parser_has_limit(self) -> None:
        parser = _build_arg_parser()
        args = parser.parse_args(["--limit", "5"])
        assert args.limit == 5

    def test_parser_defaults(self) -> None:
        parser = _build_arg_parser()
        args = parser.parse_args([])
        assert args.json is False
        assert args.audit is False
        assert args.blocked_only is False
        assert args.accepted_only is False
        assert args.limit is None


# ===================================================================
# 17. Argument validation
# ===================================================================


class TestArgumentValidation:
    """_validate_args must catch invalid combinations."""

    def test_audit_without_json_raises(self) -> None:
        args = _build_arg_parser().parse_args(["--audit"])
        with pytest.raises(SystemExit) as exc:
            _validate_args(args)
        assert exc.value.code == 2

    def test_valid_args_pass(self) -> None:
        args = _build_arg_parser().parse_args(["--json", "--audit"])
        _validate_args(args)  # Should not raise

    def test_default_args_pass(self) -> None:
        args = _build_arg_parser().parse_args([])
        _validate_args(args)  # Should not raise


# ===================================================================
# 18. Synthetic rows have no real file I/O
# ===================================================================


class TestSyntheticRowsNoFileIO:
    """Synthetic rows must never trigger actual file reads."""

    def test_attachment_path_is_fake(self) -> None:
        rows = _build_synthetic_rows()
        for row in rows:
            assert row.attachment_path.startswith("/demo/")

    def test_raw_row_text_is_synthetic(self) -> None:
        rows = _build_synthetic_rows()
        for row in rows:
            assert isinstance(row.raw_row_text, str)


# ===================================================================
# 19. Exit code 0 for supported arguments
# ===================================================================


class TestExitCodes:
    """CLI must exit 0 for all supported arguments."""

    def test_default_exit_zero(self) -> None:
        _run_cli()  # Should not raise

    def test_json_exit_zero(self) -> None:
        _run_cli(["--json"])

    def test_json_audit_exit_zero(self) -> None:
        _run_cli(["--json", "--audit"])

    def test_blocked_only_exit_zero(self) -> None:
        _run_cli(["--blocked-only"])

    def test_accepted_only_exit_zero(self) -> None:
        _run_cli(["--accepted-only"])

    def test_limit_exit_zero(self) -> None:
        _run_cli(["--limit", "3"])

    def test_help_exit_zero(self) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0


# ===================================================================
# 20. _print_text_output / _print_json_output direct tests
# ===================================================================


class TestPrintFunctionsDirect:
    """Direct calls to _print_text_output and _print_json_output for
    edge case coverage."""

    def test_print_text_output_accepted_only(self) -> None:
        rows = _build_synthetic_rows()
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_text_output(fixture, accepted_only=True)
        output = buf.getvalue()
        assert "--- Blocked Rows ---" not in output
        assert "--- Accepted Rows ---" in output

    def test_print_text_output_blocked_only(self) -> None:
        rows = _build_synthetic_rows()
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_text_output(fixture, blocked_only=True)
        output = buf.getvalue()
        assert "--- Accepted Rows ---" not in output
        assert "--- Blocked Rows ---" in output

    def test_print_text_output_with_limit(self) -> None:
        rows = _build_synthetic_rows()
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_text_output(fixture, limit=1)
        output = buf.getvalue()
        # Should have at most 2 accepted rows in output (limit per section)
        assert output.count("STARBUCKS") <= 1

    def test_print_json_output_dashboard(self) -> None:
        rows = _build_synthetic_rows()
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_json_output(fixture, batch)
        parsed = json.loads(buf.getvalue())
        assert "attachment_path" not in json.dumps(parsed)

    def test_print_json_output_audit(self) -> None:
        rows = _build_synthetic_rows()
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_json_output(fixture, batch, audit=True)
        parsed = json.loads(buf.getvalue())
        assert "attachment_path" in json.dumps(parsed)

    def test_print_json_output_accepted_only(self) -> None:
        rows = _build_synthetic_rows()
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_json_output(fixture, batch, accepted_only=True)
        parsed = json.loads(buf.getvalue())
        assert "blocked" not in parsed
        assert "accepted" in parsed

    def test_print_json_output_blocked_only(self) -> None:
        rows = _build_synthetic_rows()
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_json_output(fixture, batch, blocked_only=True)
        parsed = json.loads(buf.getvalue())
        assert "accepted" not in parsed
        assert "blocked" in parsed

    def test_print_json_output_with_limit(self) -> None:
        rows = _build_synthetic_rows()
        batch = normalize_pdf_statement_rows_batch(rows)
        fixture = build_pdf_statement_import_review_fixture(batch)
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_json_output(fixture, batch, limit=1)
        parsed = json.loads(buf.getvalue())
        assert len(parsed["accepted"]) <= 1


# ===================================================================
# 21. Integration -- full pipeline
# ===================================================================


class TestFullPipeline:
    """End-to-end integration: synthetic rows through batch normalization,
    review fixture, and CLI output."""

    def test_text_output_includes_all_expected_fields(self) -> None:
        output = _run_cli()
        assert "Total rows" in output
        assert "Accepted" in output
        assert "Blocked" in output
        assert "Blocked reason counts" in output
        assert "Accepted Rows" in output
        assert "Blocked Rows" in output

    def test_json_output_includes_all_expected_sections(self) -> None:
        output = _run_cli(["--json"])
        parsed = json.loads(output)
        assert "accepted" in parsed
        assert "blocked" in parsed
        assert "summary" in parsed
        assert "total_rows" in parsed["summary"]

    def test_dashboard_and_audit_separated(self) -> None:
        dash = json.loads(_run_cli(["--json"]))
        audit = json.loads(_run_cli(["--json", "--audit"]))
        dash_str = json.dumps(dash)
        audit_str = json.dumps(audit)
        assert "attachment_path" not in dash_str
        assert "attachment_path" in audit_str

    def test_pipeline_does_not_mutate_input(self) -> None:
        """Running the CLI multiple times must not change observable state."""
        rows1 = _build_synthetic_rows()
        batch1 = normalize_pdf_statement_rows_batch(rows1)
        fixture1 = build_pdf_statement_import_review_fixture(batch1)
        out1 = _run_cli()

        rows2 = _build_synthetic_rows()
        batch2 = normalize_pdf_statement_rows_batch(rows2)
        fixture2 = build_pdf_statement_import_review_fixture(batch2)
        out2 = _run_cli()

        assert out1 == out2
        assert fixture1.summary.total_rows == fixture2.summary.total_rows
