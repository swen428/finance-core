"""Tests for PDF Statement Review Queue Demo CLI v1."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finance_core.reconciliation.migrations import LIVE_DB_PATH
from finance_core.reconciliation.pdf_statement_review_queue_demo_cli import main
from tests.fixtures.reconciliation.pdf_statement_temp_db.generate_golden_fixtures import (
    build_text_pdf_bytes,
)

# ---------------------------------------------------------------------------
# Helper: generate a minimal text-based PDF for test use
# ---------------------------------------------------------------------------


def _create_text_based_pdf(lines: list[str], output_path: Path) -> None:
    """Create a strict deterministic text PDF for focused temporary cases."""
    output_path.write_bytes(build_text_pdf_bytes((tuple(lines),)))


# ---------------------------------------------------------------------------
# Fixture-text mode CLI tests
# ---------------------------------------------------------------------------


class TestDemoCliFixtureTextMode:
    def test_cli_default_fixture_text_mode_runs(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cli_default.sqlite"
        exit_code = main(["--db", str(db_path)])
        assert exit_code == 0
        assert db_path.exists()

    def test_cli_fixture_text_mode_json_output(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cli_fixture_json.sqlite"
        exit_code = main(["--db", str(db_path), "--json"])
        assert exit_code == 0

    def test_cli_explicit_fixture_text_source_mode(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cli_explicit_fixture.sqlite"
        exit_code = main(["--db", str(db_path), "--source-mode", "fixture-text"])
        assert exit_code == 0

    def test_cli_json_output_contains_expected_keys(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cli_keys.sqlite"
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_core.reconciliation.pdf_statement_review_queue_demo_cli",
                "--db",
                str(db_path),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["source_mode"] == "fixture_text"
        assert data["pdf_parsing_mode"] == "fixture_text"
        assert data["matched_count"] == 3
        assert data["review_required_count"] == 0
        assert data["review_only"] is True

    def test_cli_text_output_contains_source_mode(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cli_text_output.sqlite"
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_core.reconciliation.pdf_statement_review_queue_demo_cli",
                "--db",
                str(db_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "Source mode:" in result.stdout
        assert "fixture_text" in result.stdout
        assert "PDF parsing mode:" in result.stdout
        assert "Matched (imported):" in result.stdout
        assert "Review required:" in result.stdout

    def test_cli_text_output_identifies_fixture_text_not_production(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cli_not_prod.sqlite"
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_core.reconciliation.pdf_statement_review_queue_demo_cli",
                "--db",
                str(db_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "fixture_text" in result.stdout
        assert "review_only=True" in result.stdout
        assert "not_final_financial_record=True" in result.stdout


# ---------------------------------------------------------------------------
# pdf-text mode CLI tests
# ---------------------------------------------------------------------------


class TestDemoCliPdfTextMode:
    def test_cli_pdf_text_mode_runs_with_generated_pdf(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "cli_pdf_text.pdf"
        _create_text_based_pdf(
            lines=[
                "01/07/2026 CoffeeShop 12.50 D",
                "02/07/2026 Salary 1000.00 C",
            ],
            output_path=pdf_path,
        )
        db_path = tmp_path / "cli_pdf_text.sqlite"
        exit_code = main(
            [
                "--db",
                str(db_path),
                "--source-mode",
                "pdf-text",
                "--pdf-path",
                str(pdf_path),
            ]
        )
        assert exit_code == 0

    def test_cli_pdf_text_mode_json_output_shows_pdf_text(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "cli_pdf_json.pdf"
        _create_text_based_pdf(
            lines=["01/07/2026 CoffeeShop 12.50 D"],
            output_path=pdf_path,
        )
        db_path = tmp_path / "cli_pdf_json.sqlite"
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_core.reconciliation.pdf_statement_review_queue_demo_cli",
                "--db",
                str(db_path),
                "--source-mode",
                "pdf-text",
                "--pdf-path",
                str(pdf_path),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["source_mode"] == "pdf_text"
        assert data["pdf_parsing_mode"] == "pdf_text"

    def test_cli_pdf_text_mode_refuses_missing_pdf_path(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cli_missing_pdf.sqlite"
        exit_code = main(
            [
                "--db",
                str(db_path),
                "--source-mode",
                "pdf-text",
            ]
        )
        assert exit_code != 0

    def test_cli_pdf_text_mode_refuses_non_existent_pdf(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cli_nonexistent.sqlite"
        missing_pdf = tmp_path / "does_not_exist.pdf"
        exit_code = main(
            [
                "--db",
                str(db_path),
                "--source-mode",
                "pdf-text",
                "--pdf-path",
                str(missing_pdf),
            ]
        )
        assert exit_code != 0

    def test_cli_pdf_text_mode_refuses_non_pdf_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cli_not_pdf.sqlite"
        txt_file = tmp_path / "not_a_pdf.txt"
        txt_file.write_text("This is not a PDF.")
        exit_code = main(
            [
                "--db",
                str(db_path),
                "--source-mode",
                "pdf-text",
                "--pdf-path",
                str(txt_file),
            ]
        )
        assert exit_code != 0


# ---------------------------------------------------------------------------
# Live DB safety tests
# ---------------------------------------------------------------------------


class TestDemoCliLiveDbSafety:
    def test_cli_refuses_live_db_fixture_text_mode(self) -> None:
        exit_code = main(
            [
                "--db",
                str(LIVE_DB_PATH),
                "--source-mode",
                "fixture-text",
            ]
        )
        assert exit_code != 0

    def test_cli_refuses_live_db_pdf_text_mode(self) -> None:
        exit_code = main(
            [
                "--db",
                str(LIVE_DB_PATH),
                "--source-mode",
                "pdf-text",
            ]
        )
        assert exit_code != 0

    def test_cli_refuses_missing_db_arg(self) -> None:
        exit_code = main([])
        assert exit_code != 0

    def test_cli_db_flag_is_required(self) -> None:
        exit_code = main(["--source-mode", "fixture-text"])
        assert exit_code != 0

    def test_cli_refuses_memory_db_without_creating_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        exit_code = main(["--db", ":memory:", "--source-mode", "fixture-text"])

        assert exit_code != 0
        assert not (tmp_path / ":memory:").exists()


# ---------------------------------------------------------------------------
# Unsupported source mode test
# ---------------------------------------------------------------------------


class TestDemoCliUnsupportedSourceMode:
    def test_cli_refuses_unsupported_source_mode(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cli_unsupported.sqlite"
        exit_code = main(
            [
                "--db",
                str(db_path),
                "--source-mode",
                "ocr",
            ]
        )
        assert exit_code != 0


# ---------------------------------------------------------------------------
# Persistence CLI tests
# ---------------------------------------------------------------------------


class TestDemoCliPersistence:
    def test_cli_persist_review_queue_flag_works(self, tmp_path: Path) -> None:
        """--persist-review-queue flag should succeed with a file-based temp DB."""
        db_path = tmp_path / "cli_persist.sqlite"
        exit_code = main(
            [
                "--db",
                str(db_path),
                "--persist-review-queue",
                "--json",
            ]
        )
        assert exit_code == 0

        # Verify rows were persisted
        import sqlite3

        conn = sqlite3.connect(str(db_path.resolve()))
        conn.row_factory = sqlite3.Row
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM reconciliation_review_queue"
            ).fetchone()["cnt"]
            assert count > 0, f"Expected persisted rows, got {count}"
        finally:
            conn.close()

    def test_cli_persist_json_output_includes_persisted_count(self, tmp_path: Path) -> None:
        """JSON output should include persisted_count when --persist-review-queue is used."""
        db_path = tmp_path / "cli_persist_json.sqlite"
        import json
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_core.reconciliation.pdf_statement_review_queue_demo_cli",
                "--db",
                str(db_path),
                "--persist-review-queue",
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["persisted_count"] > 0, f"Expected persisted_count > 0, got {data}"
        assert "persistence_run_public_id" in data, (
            f"Expected persistence_run_public_id in output, got keys: {list(data.keys())}"
        )

    def test_cli_persist_text_output_shows_persistence_info(self, tmp_path: Path) -> None:
        """Text output should show persistence info."""
        db_path = tmp_path / "cli_persist_text.sqlite"
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_core.reconciliation.pdf_statement_review_queue_demo_cli",
                "--db",
                str(db_path),
                "--persist-review-queue",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "persisted" in result.stdout, (
            f"Expected 'persisted' in text output, got: {result.stdout[:500]}"
        )

    def test_cli_persist_refuses_live_db_in_persistence_mode(self) -> None:
        """--persist-review-queue must refuse live DB."""
        exit_code = main(
            [
                "--db",
                str(LIVE_DB_PATH),
                "--persist-review-queue",
                "--source-mode",
                "fixture-text",
            ]
        )
        assert exit_code != 0

    def test_cli_default_mode_still_in_memory(self, tmp_path: Path) -> None:
        """Default mode (no --persist-review-queue) should still be in-memory."""
        db_path = tmp_path / "cli_default_no_persist.sqlite"
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_core.reconciliation.pdf_statement_review_queue_demo_cli",
                "--db",
                str(db_path),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["persisted_count"] == 0

        # Verify no rows in reconciliation_review_queue
        import sqlite3

        conn = sqlite3.connect(str(db_path.resolve()))
        conn.row_factory = sqlite3.Row
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM reconciliation_review_queue"
            ).fetchone()["cnt"]
            assert count == 0
        finally:
            conn.close()
