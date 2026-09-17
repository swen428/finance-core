"""Tests for PDF Statement Import Run Persistence Runtime v1.

Verifies insert, idempotency, serialization, safety, and readback
of PdfStatementImportRunReviewSummary into pdf_statement_import_runs.
Every test uses an independent in-memory or temporary SQLite database
-- never touches database/finance.db or live data.
"""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from finance_core.reconciliation.migrations import TEMP_DB_MIGRATION_PATHS, apply_migration_paths
from finance_core.reconciliation.pdf_statement_import_run_persistence import (
    PdfStatementImportRunConflictError,
    PdfStatementImportRunLegacyFingerprintError,
    PdfStatementImportRunPersistenceResult,
    fetch_pdf_statement_import_run_by_public_id,
    persist_pdf_statement_import_run_review_summary,
)
from finance_core.reconciliation.pdf_statement_import_run_review_summary import (
    PdfStatementImportRunReviewSummary,
)

FROZEN_NOW = "2026-07-07T12:00:00+08:00"


class _NoRowCursor:
    def fetchone(self) -> None:
        return None


class _ForceUniqueInsertPathConnection(sqlite3.Connection):
    """Suppress one optimization lookup so the real UNIQUE conflict path runs."""

    suppress_lookup = True

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor | _NoRowCursor:
        if self.suppress_lookup and "SELECT id FROM pdf_statement_import_runs" in sql:
            self.suppress_lookup = False
            return _NoRowCursor()
        return super().execute(sql, parameters)


# -- helpers --


def _make_summary(
    *,
    run_reference: str = "run-test",
    batch_public_id: str = "batch-test",
    source_mode: str = "fixture_text",
    source_pdf_path: str = "/tmp/test.pdf",
    source_statement_id: str = "stmt-test",
    total_row_count: int = 10,
    ready_for_import_count: int = 8,
    needs_review_count: int = 2,
    blocked_count: int = 0,
    run_status: str = "fully_ready",
    blocked_reason_counts: tuple[tuple[str, int], ...] = (),
    warning_reason_counts: tuple[tuple[str, int], ...] = (),
) -> PdfStatementImportRunReviewSummary:
    return PdfStatementImportRunReviewSummary(
        run_reference=run_reference,
        batch_public_id=batch_public_id,
        source_pdf_path=source_pdf_path,
        source_statement_id=source_statement_id,
        template_id=None,
        source_mode=source_mode,
        pdf_parsing_mode=source_mode,
        total_row_count=total_row_count,
        ready_for_import_count=ready_for_import_count,
        needs_review_count=needs_review_count,
        blocked_count=blocked_count,
        blocked_reason_counts=blocked_reason_counts,
        warning_reason_counts=warning_reason_counts,
        run_status=run_status,  # type: ignore[arg-type]
    )


def create_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def apply_migration(conn: sqlite3.Connection) -> None:
    apply_migration_paths(conn, TEMP_DB_MIGRATION_PATHS)


def make_migrated_connection() -> sqlite3.Connection:
    conn = create_connection()
    apply_migration(conn)
    return conn


# ===================================================================
# 1. Insert -- fully_ready summary
# ===================================================================


class TestInsertFullyReady:
    def test_insert_fully_ready_summary(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(run_status="fully_ready")
        result = persist_pdf_statement_import_run_review_summary(conn, summary)

        assert result.inserted is True
        assert result.already_exists is False
        assert result.row_id is not None
        assert result.run_status == "fully_ready"
        assert result.total_rows == 10
        assert result.ready_for_import_rows == 8
        assert result.needs_review_rows == 2
        assert result.blocked_rows == 0
        conn.close()

    def test_insert_row_exists_in_db(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-exists",
            batch_public_id="batch-exists",
            run_status="fully_ready",
        )
        persist_pdf_statement_import_run_review_summary(conn, summary)

        row = conn.execute(
            "SELECT * FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-exists",),
        ).fetchone()
        assert row is not None
        assert row["run_public_id"] == "run-exists"
        assert row["run_status"] == "fully_ready"
        conn.close()

    def test_same_run_id_with_different_content_is_conflict(self) -> None:
        conn = make_migrated_connection()
        persist_pdf_statement_import_run_review_summary(
            conn, _make_summary(run_reference="run-conflict")
        )

        with pytest.raises(PdfStatementImportRunConflictError) as exc_info:
            persist_pdf_statement_import_run_review_summary(
                conn, _make_summary(run_reference="run-conflict", source_statement_id="changed")
            )

        assert exc_info.value.reason_code == "IDEMPOTENCY_KEY_CONTENT_CONFLICT"
        assert conn.execute("SELECT COUNT(*) FROM pdf_statement_import_runs").fetchone()[0] == 1
        conn.close()

    def test_outer_transaction_owns_rollback(self) -> None:
        conn = make_migrated_connection()
        conn.execute("BEGIN")
        persist_pdf_statement_import_run_review_summary(
            conn, _make_summary(run_reference="outer-tx")
        )
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM pdf_statement_import_runs").fetchone()[0] == 0
        conn.close()

    def test_legacy_null_fingerprint_fails_closed(self) -> None:
        conn = make_migrated_connection()
        conn.execute(
            "INSERT INTO pdf_statement_import_runs (run_public_id, source_mode, run_status, "
            "total_rows, ready_for_import_rows, needs_review_rows, blocked_rows) "
            "VALUES ('legacy', 'fixture_text', 'fully_ready', 1, 1, 0, 0)"
        )
        with pytest.raises(PdfStatementImportRunLegacyFingerprintError) as exc_info:
            persist_pdf_statement_import_run_review_summary(
                conn,
                _make_summary(
                    run_reference="legacy",
                    total_row_count=1,
                    ready_for_import_count=1,
                    needs_review_count=0,
                ),
            )
        assert exc_info.value.reason_code == "LEGACY_FINGERPRINT_UNAVAILABLE"
        conn.close()


# ===================================================================
# 2. Insert -- partially_reviewable summary
# ===================================================================


class TestInsertPartiallyReviewable:
    def test_insert_partially_reviewable_summary(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-partial",
            total_row_count=10,
            ready_for_import_count=6,
            needs_review_count=4,
            blocked_count=0,
            run_status="partially_reviewable",
        )
        result = persist_pdf_statement_import_run_review_summary(conn, summary)

        assert result.inserted is True
        assert result.run_status == "partially_reviewable"
        assert result.total_rows == 10
        assert result.ready_for_import_rows == 6
        assert result.needs_review_rows == 4
        assert result.blocked_rows == 0
        conn.close()


# ===================================================================
# 3. Insert -- blocked summary
# ===================================================================


class TestInsertBlocked:
    def test_insert_blocked_summary(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-blocked",
            total_row_count=10,
            ready_for_import_count=5,
            needs_review_count=2,
            blocked_count=3,
            run_status="blocked",
            blocked_reason_counts=(
                ("missing_date", 2),
                ("invalid_amount", 1),
            ),
        )
        result = persist_pdf_statement_import_run_review_summary(conn, summary)

        assert result.inserted is True
        assert result.run_status == "blocked"
        assert result.blocked_rows == 3
        conn.close()


# ===================================================================
# 4. Idempotency
# ===================================================================


class TestIdempotency:
    def test_repeated_persist_does_not_duplicate(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(run_reference="run-idem", run_status="fully_ready")

        r1 = persist_pdf_statement_import_run_review_summary(conn, summary)
        assert r1.inserted is True
        assert r1.already_exists is False

        r2 = persist_pdf_statement_import_run_review_summary(conn, summary)
        assert r2.inserted is False
        assert r2.already_exists is True

        # Verify only one row
        count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-idem",),
        ).fetchone()["cnt"]
        assert count == 1
        conn.close()

    def test_repeated_persist_returns_same_row_id(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(run_reference="run-same-row")

        r1 = persist_pdf_statement_import_run_review_summary(conn, summary)
        r2 = persist_pdf_statement_import_run_review_summary(conn, summary)
        assert r1.row_id == r2.row_id
        assert r2.inserted is False
        conn.close()

    @pytest.mark.parametrize(
        "changed",
        [
            {"source_pdf_path": "/different/path.pdf"},
            {"evidence_source_refs": ("different-evidence",)},
            {"persisted_count": 9},
            {"persistence_run_public_id": "retry-run"},
        ],
    )
    def test_excluded_evidence_or_persistence_metadata_replays(
        self, changed: dict[str, object]
    ) -> None:
        conn = make_migrated_connection()
        original = _make_summary(run_reference="excluded-fields")
        first = persist_pdf_statement_import_run_review_summary(conn, original)
        replay = persist_pdf_statement_import_run_review_summary(conn, replace(original, **changed))
        assert replay.inserted is False
        assert replay.row_id == first.row_id
        conn.close()

    @pytest.mark.parametrize(
        "changed",
        [
            {"total_row_count": 11, "ready_for_import_count": 9},
            {"run_status": "partially_reviewable"},
            {"source_statement_id": "other-statement"},
            {"blocked_reason_counts": (("missing_date", 1),)},
        ],
    )
    def test_material_changes_conflict(self, changed: dict[str, object]) -> None:
        conn = make_migrated_connection()
        original = _make_summary(run_reference="material-fields")
        persist_pdf_statement_import_run_review_summary(conn, original)
        with pytest.raises(PdfStatementImportRunConflictError):
            persist_pdf_statement_import_run_review_summary(conn, replace(original, **changed))
        conn.close()

    def test_different_runs_dont_collide(self) -> None:
        conn = make_migrated_connection()
        s1 = _make_summary(run_reference="run-a", run_status="fully_ready")
        s2 = _make_summary(
            run_reference="run-b",
            total_row_count=5,
            ready_for_import_count=0,
            needs_review_count=0,
            blocked_count=5,
            run_status="blocked",
        )

        r1 = persist_pdf_statement_import_run_review_summary(conn, s1)
        r2 = persist_pdf_statement_import_run_review_summary(conn, s2)

        assert r1.inserted is True
        assert r2.inserted is True
        assert r1.row_id != r2.row_id
        count = conn.execute("SELECT COUNT(*) AS cnt FROM pdf_statement_import_runs").fetchone()[
            "cnt"
        ]
        assert count == 2
        conn.close()

    def test_two_connections_same_content_reloads_after_unique_conflict(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "pdf-race.sqlite"
        first = sqlite3.connect(path)
        first.row_factory = sqlite3.Row
        apply_migration(first)
        first_result = persist_pdf_statement_import_run_review_summary(
            first, _make_summary(run_reference="race-same")
        )
        first.commit()
        second = sqlite3.connect(path, factory=_ForceUniqueInsertPathConnection)
        second.row_factory = sqlite3.Row
        try:
            replay = persist_pdf_statement_import_run_review_summary(
                second, _make_summary(run_reference="race-same")
            )
            assert replay.inserted is False
            assert replay.row_id == first_result.row_id
            count = second.execute("SELECT COUNT(*) FROM pdf_statement_import_runs").fetchone()[0]
            assert count == 1
        finally:
            second.close()
            first.close()

    def test_two_connections_different_content_conflicts_after_unique_conflict(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "pdf-race-conflict.sqlite"
        first = sqlite3.connect(path)
        first.row_factory = sqlite3.Row
        apply_migration(first)
        persist_pdf_statement_import_run_review_summary(
            first, _make_summary(run_reference="race-diff")
        )
        first.commit()
        second = sqlite3.connect(path, factory=_ForceUniqueInsertPathConnection)
        second.row_factory = sqlite3.Row
        try:
            with pytest.raises(PdfStatementImportRunConflictError) as exc_info:
                persist_pdf_statement_import_run_review_summary(
                    second, _make_summary(run_reference="race-diff", source_statement_id="other")
                )
            assert exc_info.value.reason_code == "IDEMPOTENCY_KEY_CONTENT_CONFLICT"
            count = second.execute("SELECT COUNT(*) FROM pdf_statement_import_runs").fetchone()[0]
            assert count == 1
        finally:
            second.close()
            first.close()


# ===================================================================
# 5. Count matching
# ===================================================================


class TestCountMatching:
    def test_persisted_counts_match_summary(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-counts",
            total_row_count=20,
            ready_for_import_count=14,
            needs_review_count=3,
            blocked_count=3,
            run_status="blocked",
        )
        persist_pdf_statement_import_run_review_summary(conn, summary)

        row = conn.execute(
            "SELECT * FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-counts",),
        ).fetchone()
        assert row["total_rows"] == 20
        assert row["ready_for_import_rows"] == 14
        assert row["needs_review_rows"] == 3
        assert row["blocked_rows"] == 3
        conn.close()

    def test_row_count_sum_consistency(self) -> None:
        """Verify that valid summaries satisfy migration 017 CHECK constraint."""
        conn = make_migrated_connection()
        # total_rows = ready + needs_review + blocked
        summary = _make_summary(
            run_reference="run-sum-ok",
            total_row_count=10,
            ready_for_import_count=5,
            needs_review_count=3,
            blocked_count=2,
        )
        result = persist_pdf_statement_import_run_review_summary(conn, summary)
        assert result.inserted is True
        conn.close()


# ===================================================================
# 6. Status matching
# ===================================================================


class TestStatusMatching:
    @pytest.mark.parametrize(
        "status",
        ["fully_ready", "partially_reviewable", "blocked"],
    )
    def test_status_persisted(self, status: str) -> None:
        conn = make_migrated_connection()
        overrides: dict[str, object] = {}
        if status == "partially_reviewable":
            overrides = {
                "total_row_count": 10,
                "ready_for_import_count": 5,
                "needs_review_count": 5,
                "blocked_count": 0,
            }
        elif status == "blocked":
            overrides = {
                "total_row_count": 10,
                "ready_for_import_count": 5,
                "needs_review_count": 3,
                "blocked_count": 2,
            }

        summary = _make_summary(
            run_reference=f"run-status-{status}",
            run_status=status,
            **overrides,  # type: ignore[arg-type]
        )
        result = persist_pdf_statement_import_run_review_summary(conn, summary)
        assert result.run_status == status
        conn.close()


# ===================================================================
# 7. Source mode preservation
# ===================================================================


class TestSourceModePreservation:
    def test_fixture_text_preserved(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-mode-fixture",
            source_mode="fixture_text",
        )
        persist_pdf_statement_import_run_review_summary(conn, summary)

        row = conn.execute(
            "SELECT source_mode FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-mode-fixture",),
        ).fetchone()
        assert row["source_mode"] == "fixture_text"
        conn.close()

    def test_pdf_text_preserved(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-mode-pdf",
            source_mode="pdf_text",
        )
        persist_pdf_statement_import_run_review_summary(conn, summary)

        row = conn.execute(
            "SELECT source_mode FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-mode-pdf",),
        ).fetchone()
        assert row["source_mode"] == "pdf_text"
        conn.close()


# ===================================================================
# 8. Source evidence preservation
# ===================================================================


class TestSourceEvidencePreservation:
    def test_source_pdf_path_preserved(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-ev-path",
            source_pdf_path="/data/stmt_2026_07.pdf",
        )
        persist_pdf_statement_import_run_review_summary(conn, summary)

        row = conn.execute(
            "SELECT source_pdf_path FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-ev-path",),
        ).fetchone()
        assert row["source_pdf_path"] == "/data/stmt_2026_07.pdf"
        conn.close()

    def test_source_statement_id_preserved(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-ev-stmt",
            source_statement_id="pdf-tmpl-cli-a1b2c3d4e5f6",
        )
        persist_pdf_statement_import_run_review_summary(conn, summary)

        row = conn.execute(
            "SELECT source_statement_id FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-ev-stmt",),
        ).fetchone()
        assert row["source_statement_id"] == "pdf-tmpl-cli-a1b2c3d4e5f6"
        conn.close()


# ===================================================================
# 9. blocked_reason_counts_json deterministic
# ===================================================================


class TestBlockedReasonCountsJson:
    def test_blocked_reason_counts_deterministic(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-blocked-json",
            total_row_count=10,
            ready_for_import_count=4,
            needs_review_count=2,
            blocked_count=4,
            run_status="blocked",
            blocked_reason_counts=(
                ("missing_date", 2),
                ("invalid_amount", 1),
                ("missing_description", 1),
            ),
        )
        persist_pdf_statement_import_run_review_summary(conn, summary)

        # Persist again -- same JSON should be produced
        r2 = persist_pdf_statement_import_run_review_summary(conn, summary)
        assert r2.inserted is False

        row = conn.execute(
            "SELECT blocked_reason_counts_json "
            "FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-blocked-json",),
        ).fetchone()

        parsed = json.loads(row["blocked_reason_counts_json"])
        assert parsed == {
            "invalid_amount": 1,
            "missing_date": 2,
            "missing_description": 1,
        }
        conn.close()

    def test_empty_blocked_reason_counts(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-empty-blocked",
            blocked_reason_counts=(),
        )
        persist_pdf_statement_import_run_review_summary(conn, summary)

        row = conn.execute(
            "SELECT blocked_reason_counts_json "
            "FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-empty-blocked",),
        ).fetchone()
        assert row["blocked_reason_counts_json"] == "{}"
        conn.close()


# ===================================================================
# 10. warning_reason_counts_json deterministic
# ===================================================================


class TestWarningReasonCountsJson:
    def test_warning_reason_counts_deterministic(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-warn-json",
            total_row_count=8,
            ready_for_import_count=5,
            needs_review_count=3,
            blocked_count=0,
            run_status="partially_reviewable",
            warning_reason_counts=(
                ("date_ambiguous", 2),
                ("low_confidence", 1),
            ),
        )
        persist_pdf_statement_import_run_review_summary(conn, summary)

        row = conn.execute(
            "SELECT warning_reason_counts_json "
            "FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-warn-json",),
        ).fetchone()

        parsed = json.loads(row["warning_reason_counts_json"])
        assert parsed == {"date_ambiguous": 2, "low_confidence": 1}
        conn.close()

    def test_empty_warning_reason_counts(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-empty-warn",
            warning_reason_counts=(),
        )
        persist_pdf_statement_import_run_review_summary(conn, summary)

        row = conn.execute(
            "SELECT warning_reason_counts_json "
            "FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-empty-warn",),
        ).fetchone()
        assert row["warning_reason_counts_json"] == "{}"
        conn.close()


# ===================================================================
# 11. Dashboard vs audit JSON separation
# ===================================================================


class TestDashboardAuditSeparation:
    def test_dashboard_excludes_audit_fields(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-dash-audit",
            source_pdf_path="/secret/stmt.pdf",
            source_statement_id="secret-id",
        )
        persist_pdf_statement_import_run_review_summary(conn, summary)

        row = conn.execute(
            "SELECT dashboard_summary_json, audit_summary_json "
            "FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-dash-audit",),
        ).fetchone()

        dashboard = json.loads(row["dashboard_summary_json"])
        audit = json.loads(row["audit_summary_json"])

        # Dashboard must exclude source evidence
        assert "source_pdf_path" not in dashboard
        assert "source_statement_id" not in dashboard
        assert "evidence_source_refs" not in dashboard

        # Audit must include source evidence
        assert audit["source_pdf_path"] == "/secret/stmt.pdf"
        assert audit["source_statement_id"] == "secret-id"
        assert "evidence_source_refs" in audit
        conn.close()


# ===================================================================
# 12. Invalid source_mode/status not silently transformed
# ===================================================================


class TestInvalidValuesRejected:
    def test_invalid_source_mode_rejected(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-bad-mode",
            source_mode="ocr_text",
        )
        with pytest.raises(sqlite3.IntegrityError):
            persist_pdf_statement_import_run_review_summary(conn, summary)
        conn.close()

    def test_invalid_run_status_rejected(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-bad-status",
            run_status="pending",
        )
        with pytest.raises(sqlite3.IntegrityError):
            persist_pdf_statement_import_run_review_summary(conn, summary)
        conn.close()


# ===================================================================
# 13. fetch helper
# ===================================================================


class TestFetchHelper:
    def test_fetch_returns_persisted_row(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-fetch",
            batch_public_id="batch-fetch",
            source_mode="pdf_text",
            source_pdf_path="/data/ocbc.pdf",
            source_statement_id="pdf-tmpl-cli-abc",
            total_row_count=15,
            ready_for_import_count=10,
            needs_review_count=3,
            blocked_count=2,
            run_status="blocked",
        )
        persist_pdf_statement_import_run_review_summary(
            conn,
            summary,
            created_at=FROZEN_NOW,
        )

        fetched = fetch_pdf_statement_import_run_by_public_id(conn, "run-fetch")
        assert fetched is not None
        assert fetched["run_public_id"] == "run-fetch"
        assert fetched["import_batch_public_id"] == "batch-fetch"
        assert fetched["source_mode"] == "pdf_text"
        assert fetched["source_pdf_path"] == "/data/ocbc.pdf"
        assert fetched["source_statement_id"] == "pdf-tmpl-cli-abc"
        assert fetched["run_status"] == "blocked"
        assert fetched["total_rows"] == 15
        assert fetched["ready_for_import_rows"] == 10
        assert fetched["needs_review_rows"] == 3
        assert fetched["blocked_rows"] == 2
        assert fetched["created_at"] == FROZEN_NOW
        conn.close()

    def test_fetch_returns_none_for_missing(self) -> None:
        conn = make_migrated_connection()
        fetched = fetch_pdf_statement_import_run_by_public_id(conn, "no-such-run")
        assert fetched is None
        conn.close()


# ===================================================================
# 14. created_at handling
# ===================================================================


class TestCreatedAt:
    def test_explicit_created_at_preserved(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(run_reference="run-with-ts")
        persist_pdf_statement_import_run_review_summary(
            conn,
            summary,
            created_at=FROZEN_NOW,
        )

        row = conn.execute(
            "SELECT created_at FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-with-ts",),
        ).fetchone()
        assert row["created_at"] == FROZEN_NOW
        conn.close()

    def test_default_created_at_produced(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(run_reference="run-default-ts")
        persist_pdf_statement_import_run_review_summary(conn, summary)

        row = conn.execute(
            "SELECT created_at FROM pdf_statement_import_runs WHERE run_public_id = ?",
            ("run-default-ts",),
        ).fetchone()
        assert row["created_at"] is not None
        assert isinstance(row["created_at"], str)
        assert len(row["created_at"]) > 0
        conn.close()


# ===================================================================
# 15. Readback round-trip
# ===================================================================


class TestReadbackRoundTrip:
    def test_full_round_trip(self) -> None:
        conn = make_migrated_connection()
        summary = _make_summary(
            run_reference="run-roundtrip",
            batch_public_id="batch-roundtrip",
            source_mode="pdf_text",
            source_pdf_path="/data/stmt.pdf",
            source_statement_id="pdf-tmpl-cli-xyz",
            total_row_count=12,
            ready_for_import_count=8,
            needs_review_count=2,
            blocked_count=2,
            run_status="blocked",
            blocked_reason_counts=(("missing_date", 2),),
            warning_reason_counts=(("date_inferred", 1),),
        )
        persist_pdf_statement_import_run_review_summary(
            conn,
            summary,
            created_at=FROZEN_NOW,
        )

        fetched = fetch_pdf_statement_import_run_by_public_id(conn, "run-roundtrip")
        assert fetched is not None
        assert fetched["run_public_id"] == "run-roundtrip"
        assert fetched["total_rows"] == 12
        assert fetched["ready_for_import_rows"] == 8
        assert fetched["needs_review_rows"] == 2
        assert fetched["blocked_rows"] == 2
        assert fetched["run_status"] == "blocked"

        # JSON fields
        blocked = json.loads(fetched["blocked_reason_counts_json"])
        assert blocked == {"missing_date": 2}
        warning = json.loads(fetched["warning_reason_counts_json"])
        assert warning == {"date_inferred": 1}

        # Dashboard and audit snapshots present
        assert fetched["dashboard_summary_json"] is not None
        assert fetched["audit_summary_json"] is not None
        conn.close()


# ===================================================================
# 16. Safety: no live DB, no final transaction tables
# ===================================================================


class TestSafetyConstraints:
    def test_no_live_db_reference(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_persistence as mod

        source = inspect.getsource(mod)
        # Strip module docstring -- non-goals mention finance.db legitimately
        code_only = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        assert "finance.db" not in code_only

    def test_no_transaction_tables_touched(self) -> None:
        """Verify our persistence does not create final transaction tables."""
        conn = make_migrated_connection()
        summary = _make_summary()
        persist_pdf_statement_import_run_review_summary(conn, summary)

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
        conn.close()

    def test_no_telegram_ocr_metabase_imports(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_persistence as mod

        source = inspect.getsource(mod)
        # Strip module docstring -- non-goals mention these terms legitimately
        code_only = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        for dep in ("telegram", "ocr", "metabase"):
            assert dep not in code_only.lower(), f"Unexpected {dep} reference"

    def test_no_settlement_import(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_persistence as mod

        source = inspect.getsource(mod)
        # Strip module docstring -- non-goals mention settlement legitimately
        code_only = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        assert "settlement" not in code_only.lower()


# ===================================================================
# 17. Immutability
# ===================================================================


class TestImmutability:
    def test_result_is_frozen(self) -> None:
        result = PdfStatementImportRunPersistenceResult(
            run_public_id="run-immutable",
            inserted=True,
            already_exists=False,
            row_id=1,
            run_status="fully_ready",
            total_rows=10,
            ready_for_import_rows=8,
            needs_review_rows=2,
            blocked_rows=0,
        )
        with pytest.raises(Exception):
            result.inserted = False  # type: ignore[misc]
        with pytest.raises(Exception):
            result.run_public_id = "mutated"  # type: ignore[misc]


# ===================================================================
# 18. Public API shape
# ===================================================================


class TestPublicAPI:
    def test_all_exports(self) -> None:
        from finance_core.reconciliation import pdf_statement_import_run_persistence as mod

        assert hasattr(mod, "__all__")
        expected = {
            "PdfStatementImportRunConflictError",
            "PdfStatementImportRunLegacyFingerprintError",
            "PdfStatementImportRunPersistenceResult",
            "persist_pdf_statement_import_run_review_summary",
            "fetch_pdf_statement_import_run_by_public_id",
        }
        assert set(mod.__all__) == expected
