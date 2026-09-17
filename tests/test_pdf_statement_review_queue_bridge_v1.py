"""Tests for PDF Statement Review Queue Bridge v1."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from finance_core.reconciliation.migrations import LIVE_DB_PATH
from finance_core.reconciliation.pdf_statement_review_queue_bridge import (
    PdfStatementReviewQueueBridgeResult,
    bridge_result_to_summary_dict,
    run_pdf_statement_review_queue_bridge,
)
from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
    DEFAULT_PDF_FIXTURE_PATH,
)
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
# Bridge fixture_text mode tests
# ---------------------------------------------------------------------------


class TestBridgeFixtureTextMode:
    def test_bridge_default_fixture_text_mode_works(self, tmp_path: Path) -> None:
        db_path = tmp_path / "bridge_default.sqlite"
        result = run_pdf_statement_review_queue_bridge(db_path=db_path)

        assert isinstance(result, PdfStatementReviewQueueBridgeResult)
        assert result.source_mode == "fixture_text"
        assert result.pdf_parsing_mode == "fixture_text"
        assert result.matched_count == 3
        assert result.review_required_count == 0
        assert result.ready_for_import_count == 3
        assert result.blocked_count == 0
        assert result.review_only is True
        assert result.not_final_financial_record is True

    def test_bridge_explicit_fixture_text_mode(self, tmp_path: Path) -> None:
        db_path = tmp_path / "bridge_explicit_fixture.sqlite"
        result = run_pdf_statement_review_queue_bridge(
            db_path=db_path,
            source_mode="fixture_text",
        )
        assert result.source_mode == "fixture_text"
        assert result.pdf_parsing_mode == "fixture_text"
        assert result.matched_count == 3

    def test_bridge_fixture_text_mode_summary_dict(self, tmp_path: Path) -> None:
        db_path = tmp_path / "bridge_summary.sqlite"
        result = run_pdf_statement_review_queue_bridge(db_path=db_path)
        summary = bridge_result_to_summary_dict(result)

        assert summary["source_mode"] == "fixture_text"
        assert summary["pdf_parsing_mode"] == "fixture_text"
        assert summary["matched_count"] == 3
        assert summary["review_required_count"] == 0
        assert summary["review_only"] is True
        assert summary["not_final_financial_record"] is True
        assert "db_path" in summary
        assert "pdf_path" in summary

    def test_bridge_preserves_source_attachment_path(self, tmp_path: Path) -> None:
        db_path = tmp_path / "bridge_source_path.sqlite"
        result = run_pdf_statement_review_queue_bridge(db_path=db_path)
        assert result.import_result.pdf_path == str(DEFAULT_PDF_FIXTURE_PATH.resolve())

        # Check that the review queue also preserves attachment path
        assert result.review_queue.summary.attachment_path == str(
            DEFAULT_PDF_FIXTURE_PATH.resolve()
        )

    def test_bridge_db_has_rows(self, tmp_path: Path) -> None:
        db_path = tmp_path / "bridge_db_rows.sqlite"
        _ = run_pdf_statement_review_queue_bridge(db_path=db_path)

        conn = sqlite3.connect(str(db_path.resolve()))
        conn.row_factory = sqlite3.Row
        try:
            c = conn.execute("SELECT COUNT(*) AS cnt FROM statement_transactions").fetchone()
            assert c["cnt"] == 3
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Bridge pdf_text mode tests
# ---------------------------------------------------------------------------


class TestBridgePdfTextMode:
    def test_bridge_pdf_text_mode_with_generated_pdf(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "bridge_pdf_text.pdf"
        _create_text_based_pdf(
            lines=[
                "01/07/2026 CoffeeShop 12.50 D",
                "02/07/2026 Salary 1000.00 C",
            ],
            output_path=pdf_path,
        )
        db_path = tmp_path / "bridge_pdf_text.sqlite"
        result = run_pdf_statement_review_queue_bridge(
            db_path=db_path,
            source_mode="pdf_text",
            pdf_path=pdf_path,
        )
        assert result.source_mode == "pdf_text"
        assert result.pdf_parsing_mode == "pdf_text"
        assert result.matched_count >= 1

    def test_bridge_pdf_text_mode_preserves_source_pdf_path(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "src_path_br.pdf"
        _create_text_based_pdf(
            lines=["01/07/2026 CoffeeShop 12.50 D"],
            output_path=pdf_path,
        )
        db_path = tmp_path / "bridge_src_path.sqlite"
        result = run_pdf_statement_review_queue_bridge(
            db_path=db_path,
            source_mode="pdf_text",
            pdf_path=pdf_path,
        )
        assert result.import_result.pdf_path == str(pdf_path.resolve())
        assert result.review_queue.summary.attachment_path == str(pdf_path.resolve())

    def test_bridge_pdf_text_mode_reports_parsing_mode_in_summary(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "parsing_mode_br.pdf"
        _create_text_based_pdf(
            lines=["01/07/2026 CoffeeShop 12.50 D"],
            output_path=pdf_path,
        )
        result = run_pdf_statement_review_queue_bridge(
            db_path=tmp_path / "parsing_mode_br.sqlite",
            source_mode="pdf_text",
            pdf_path=pdf_path,
        )
        summary = bridge_result_to_summary_dict(result)
        assert summary["pdf_parsing_mode"] == "pdf_text"

    def test_bridge_pdf_text_mode_no_silent_fallback(self, tmp_path: Path) -> None:
        """pdf_text mode with missing PDF must raise, not fall back."""
        missing_pdf = tmp_path / "does_not_exist.pdf"
        with pytest.raises(Exception):
            run_pdf_statement_review_queue_bridge(
                db_path=tmp_path / "nope_br.sqlite",
                source_mode="pdf_text",
                pdf_path=missing_pdf,
            )

    def test_bridge_pdf_text_checked_in_golden_pdf(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "br_golden_test.sqlite")
            result = run_pdf_statement_review_queue_bridge(
                db_path=db_path,
                source_mode="pdf_text",
                pdf_path=DEFAULT_PDF_FIXTURE_PATH,
            )
            assert result.ready_for_import_count == 3
            assert result.matched_count == 3


# ---------------------------------------------------------------------------
# Live DB safety tests
# ---------------------------------------------------------------------------


class TestLiveDbSafety:
    def test_refuses_live_db_in_fixture_text_mode(self) -> None:
        with pytest.raises(ValueError, match="Refusing to use database/finance.db"):
            run_pdf_statement_review_queue_bridge(
                db_path=LIVE_DB_PATH,
                source_mode="fixture_text",
            )

    def test_refuses_live_db_in_pdf_text_mode(self) -> None:
        with pytest.raises(ValueError, match="Refusing to use database/finance.db"):
            run_pdf_statement_review_queue_bridge(
                db_path=LIVE_DB_PATH,
                source_mode="pdf_text",
            )


# ---------------------------------------------------------------------------
# Source mode / metadata tests
# ---------------------------------------------------------------------------


class TestSourceModeMetadata:
    def test_default_source_mode_is_fixture_text(self, tmp_path: Path) -> None:
        result = run_pdf_statement_review_queue_bridge(
            db_path=tmp_path / "default_meta.sqlite",
        )
        assert result.source_mode == "fixture_text"
        assert result.pdf_parsing_mode == "fixture_text"

    def test_explicit_pdf_text_source_mode(self, tmp_path: Path) -> None:
        pdf_path = tmp_path / "expl_meta.pdf"
        _create_text_based_pdf(
            lines=["01/07/2026 CoffeeShop 12.50 D"],
            output_path=pdf_path,
        )
        result = run_pdf_statement_review_queue_bridge(
            db_path=tmp_path / "expl_meta.sqlite",
            source_mode="pdf_text",
            pdf_path=pdf_path,
        )
        assert result.source_mode == "pdf_text"
        assert result.pdf_parsing_mode == "pdf_text"


# ---------------------------------------------------------------------------
# Persistence bridge tests
# ---------------------------------------------------------------------------


class TestBridgePersistence:
    def test_default_mode_does_not_persist(self, tmp_path: Path) -> None:
        """Default bridge mode should return persisted_count==0 and not write to review queue."""
        db_path = tmp_path / "bridge_no_persist.sqlite"
        result = run_pdf_statement_review_queue_bridge(db_path=db_path)

        assert result.persisted_count == 0
        assert result.persistence_run_public_id is None

        # Verify no rows in reconciliation_review_queue
        conn = sqlite3.connect(str(db_path.resolve()))
        conn.row_factory = sqlite3.Row
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM reconciliation_review_queue"
            ).fetchone()["cnt"]
            assert count == 0
        finally:
            conn.close()

    def test_persist_review_queue_persists_rows(self, tmp_path: Path) -> None:
        """persist_review_queue=True should write rows to reconciliation_review_queue."""
        db_path = tmp_path / "bridge_persist.sqlite"
        result = run_pdf_statement_review_queue_bridge(
            db_path=db_path,
            persist_review_queue=True,
        )

        assert result.persisted_count > 0
        assert result.persistence_run_public_id is not None

        # Verify rows exist in reconciliation_review_queue
        conn = sqlite3.connect(str(db_path.resolve()))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM reconciliation_review_queue").fetchall()
            assert len(rows) == result.persisted_count

            # Verify run_public_id is set
            for row in rows:
                assert row["run_public_id"] == result.persistence_run_public_id
        finally:
            conn.close()

    def test_persist_review_queue_includes_review_required_items(self, tmp_path: Path) -> None:
        """Persisted rows must include at least one review-required (unmatched) item."""
        db_path = tmp_path / "bridge_review_required.sqlite"
        _result = run_pdf_statement_review_queue_bridge(
            db_path=db_path,
            persist_review_queue=True,
        )

        conn = sqlite3.connect(str(db_path.resolve()))
        conn.row_factory = sqlite3.Row
        try:
            # Find rows where issue_type is not "matched"
            non_matched = conn.execute(
                """
                SELECT issue_type, COUNT(*) AS cnt
                FROM reconciliation_review_queue
                WHERE issue_type != 'matched'
                GROUP BY issue_type
                """
            ).fetchall()
            total_non_matched = sum(r["cnt"] for r in non_matched)
            assert total_non_matched >= 1, (
                "Expected at least one review-required item, "
                f"got types: {[(r['issue_type'], r['cnt']) for r in non_matched]}"
            )

            # Verify at least one row has a review-required issue type.
            # The Grocer row (45.67) has no matching app transaction by amount,
            # so the deterministic matcher classifies it as AMOUNT_MISMATCH.
            review_required_types = (
                "missing_in_app",
                "missing_in_statement",
                "no_match",
                "amount_mismatch",
                "currency_mismatch",
                "date_mismatch",
                "merchant_mismatch",
                "needs_review",
            )
            specific = conn.execute(
                f"""
                SELECT * FROM reconciliation_review_queue
                WHERE issue_type IN ({",".join("?" for _ in review_required_types)})
                """,
                review_required_types,
            ).fetchall()
            assert len(specific) >= 1, (
                f"Expected at least one missing/unmatched item, got {len(specific)}"
            )
        finally:
            conn.close()

    def test_persist_review_queue_preserves_source_evidence(self, tmp_path: Path) -> None:
        """Persisted rows must include source/evidence traceability."""
        db_path = tmp_path / "bridge_source_ev.sqlite"
        _result = run_pdf_statement_review_queue_bridge(
            db_path=db_path,
            persist_review_queue=True,
        )

        conn = sqlite3.connect(str(db_path.resolve()))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT * FROM reconciliation_review_queue
                WHERE issue_type = 'matched'
                LIMIT 1
                """
            ).fetchall()
            assert len(rows) >= 1, "Expected at least one matched persisted row"
            matched_row = rows[0]

            # evidence_json should contain statement/merchant details
            evidence_text = matched_row["evidence_json"] or "{}"
            assert "statement_merchant" in evidence_text, (
                f"Expected source merchant in evidence, got: {evidence_text}"
            )

            # statement_transaction_ref should be non-None/empty
            stmt_ref = matched_row["statement_transaction_ref"]
            assert stmt_ref, f"Expected statement_transaction_ref to be set, got: {stmt_ref}"
        finally:
            conn.close()

    def test_persist_summary_dict_includes_persisted_count(self, tmp_path: Path) -> None:
        """bridge_result_to_summary_dict should include persisted_count."""
        db_path = tmp_path / "bridge_summary_persist.sqlite"
        result = run_pdf_statement_review_queue_bridge(
            db_path=db_path,
            persist_review_queue=True,
        )
        summary = bridge_result_to_summary_dict(result)
        assert summary["persisted_count"] == result.persisted_count
        assert summary.get("persistence_run_public_id") == result.persistence_run_public_id

    def test_persist_refuses_memory_db(self) -> None:
        """Persistence mode must refuse :memory: DB."""
        with pytest.raises(ValueError, match=":memory:"):
            run_pdf_statement_review_queue_bridge(
                db_path=":memory:",
                persist_review_queue=True,
            )

    def test_persist_refuses_live_db(self) -> None:
        """Persistence mode must still refuse live DB."""
        with pytest.raises(ValueError, match="Refusing to use database/finance.db"):
            run_pdf_statement_review_queue_bridge(
                db_path=LIVE_DB_PATH,
                persist_review_queue=True,
            )

    def test_persist_review_queue_is_idempotent_for_same_batch(self, tmp_path: Path) -> None:
        """Re-running persistence for the same batch should not duplicate rows."""
        db_path = tmp_path / "bridge_idempotent.sqlite"

        first = run_pdf_statement_review_queue_bridge(
            db_path=db_path,
            persist_review_queue=True,
        )
        second = run_pdf_statement_review_queue_bridge(
            db_path=db_path,
            persist_review_queue=True,
        )

        assert second.persisted_count == first.persisted_count
        assert second.persistence_run_public_id == first.persistence_run_public_id

        conn = sqlite3.connect(str(db_path.resolve()))
        conn.row_factory = sqlite3.Row
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM reconciliation_review_queue"
            ).fetchone()["cnt"]
            assert count == first.persisted_count
        finally:
            conn.close()

    def test_persist_review_queue_scopes_to_current_batch(self, tmp_path: Path) -> None:
        """Two batches in one temp DB should each persist only their own rows."""
        db_path = tmp_path / "bridge_two_batches.sqlite"
        pdf_a = tmp_path / "statement_a.pdf"
        pdf_b = tmp_path / "statement_b.pdf"
        shutil.copyfile(DEFAULT_PDF_FIXTURE_PATH, pdf_a)
        shutil.copyfile(DEFAULT_PDF_FIXTURE_PATH, pdf_b)
        pdf_b.write_bytes(pdf_b.read_bytes() + b"\n% distinct synthetic fixture bytes\n")

        first = run_pdf_statement_review_queue_bridge(
            db_path=db_path,
            pdf_path=pdf_a,
            batch_public_id="batch-a",
            persist_review_queue=True,
            persistence_run_public_id="run-a",
        )
        second = run_pdf_statement_review_queue_bridge(
            db_path=db_path,
            pdf_path=pdf_b,
            batch_public_id="batch-b",
            persist_review_queue=True,
            persistence_run_public_id="run-b",
        )

        assert first.persisted_count == 3
        assert second.persisted_count == 3

        conn = sqlite3.connect(str(db_path.resolve()))
        conn.row_factory = sqlite3.Row
        try:
            counts = {
                row["run_public_id"]: row["cnt"]
                for row in conn.execute(
                    """
                    SELECT run_public_id, COUNT(*) AS cnt
                    FROM reconciliation_review_queue
                    GROUP BY run_public_id
                    """
                ).fetchall()
            }
            assert counts == {"run-a": 3, "run-b": 3}

            refs = conn.execute(
                """
                SELECT rq.statement_transaction_ref
                FROM reconciliation_review_queue rq
                LEFT JOIN statement_transactions st
                  ON st.public_id = rq.statement_transaction_ref
                WHERE st.id IS NULL
                """
            ).fetchall()
            assert refs == []
        finally:
            conn.close()
