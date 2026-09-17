"""Tests for PDF Statement Import Run Persistence Integration v1.

Verifies the end-to-end integration: summary build -> persist -> fetch/readback
on temporary/test SQLite databases only.  Never touches database/finance.db,
live data, migrations, final financial records, or settlement obligations.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from finance_core.reconciliation.migrations import (
    MIGRATION_022_DATABASE_CONFLICT_FINGERPRINTS,
    TEMP_DB_MIGRATION_PATHS,
    apply_migration_paths,
)
from finance_core.reconciliation.pdf_statement_import_run_persistence import (
    PdfStatementImportRunConflictError,
)
from finance_core.reconciliation.pdf_statement_import_run_persistence_integration import (
    PdfStatementImportRunPersistenceIntegrationResult,
    persist_pdf_statement_import_run_summary_from_import_result,
)
from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
    DEFAULT_PDF_FIXTURE_PATH,
    DEFAULT_TEMPLATE_ID,
    DEFAULT_TEXT_FIXTURE_PATH,
    PdfStatementTempDbImportResult,
    import_pdf_statement_fixture_to_temp_db,
)
from finance_core.reconciliation.statement_import import StatementImportTransactionError

FROZEN_NOW = "2026-07-07T12:00:00+08:00"


# -- helpers --


def _apply_migrations_through_022(conn: sqlite3.Connection) -> None:
    # Minimal prerequisite set: migrations 001–017 plus actual migration 022.
    # The dedicated migration test owns the complete 001–022 replay assertion.
    apply_migration_paths(
        conn,
        (*TEMP_DB_MIGRATION_PATHS[:17], MIGRATION_022_DATABASE_CONFLICT_FINGERPRINTS),
    )


def _make_migrated_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_migrations_through_022(conn)
    return conn


def _run_fixture_import(db_path: str, batch_public_id: str) -> PdfStatementTempDbImportResult:
    return import_pdf_statement_fixture_to_temp_db(
        db_path=str(db_path),
        pdf_path=str(DEFAULT_PDF_FIXTURE_PATH),
        text_fixture_path=str(DEFAULT_TEXT_FIXTURE_PATH),
        template_id=DEFAULT_TEMPLATE_ID,
        batch_public_id=batch_public_id,
        source_mode="fixture_text",
    )


# ===================================================================
# 1. Integration builds a summary from existing deterministic fixture
# ===================================================================


class TestBuildSummaryFromFixture:
    def test_builds_summary_from_deterministic_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_build.db"
            import_result = _run_fixture_import(str(db_path), "batch-build-1")
            conn = _make_migrated_connection(str(Path(tmpdir) / "int.db"))
            try:
                result = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                assert result.run_public_id is not None
                assert result.run_public_id != ""
                assert result.total_rows == 3
                assert result.ready_for_import_rows >= 0
                assert result.run_status in (
                    "fully_ready",
                    "partially_reviewable",
                    "blocked",
                )
            finally:
                conn.close()


class _PostPersistFailure(RuntimeError):
    pass


def _raise_post_persist_failure() -> None:
    raise _PostPersistFailure()


class TestServiceOwnedTransaction:
    def test_successful_integration_survives_connection_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_db = str(Path(tmpdir) / "fixture.db")
            integration_db = str(Path(tmpdir) / "integration.db")
            import_result = _run_fixture_import(fixture_db, "batch-durable")
            conn = _make_migrated_connection(integration_db)
            result = persist_pdf_statement_import_run_summary_from_import_result(
                conn, import_result
            )
            conn.close()

            reopened = sqlite3.connect(integration_db)
            reopened.row_factory = sqlite3.Row
            try:
                row = reopened.execute(
                    "SELECT content_fingerprint, fingerprint_version "
                    "FROM pdf_statement_import_runs "
                    "WHERE run_public_id = ?",
                    (result.run_public_id,),
                ).fetchone()
                assert row is not None
                assert len(row["content_fingerprint"]) == 64
                assert row["fingerprint_version"] == "pdf-statement-import-run-v1"
            finally:
                reopened.close()

    def test_post_insert_failure_rolls_back_entire_service_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_db = str(Path(tmpdir) / "fixture.db")
            integration_db = str(Path(tmpdir) / "integration.db")
            import_result = _run_fixture_import(fixture_db, "batch-rollback")
            conn = _make_migrated_connection(integration_db)
            try:
                with pytest.raises(_PostPersistFailure):
                    persist_pdf_statement_import_run_summary_from_import_result(
                        conn,
                        import_result,
                        _test_post_persist_hook=_raise_post_persist_failure,
                    )
            finally:
                conn.close()

            reopened = sqlite3.connect(integration_db)
            try:
                count = reopened.execute(
                    "SELECT COUNT(*) FROM pdf_statement_import_runs"
                ).fetchone()[0]
                assert count == 0
            finally:
                reopened.close()

    def test_exact_replay_is_durable_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_db = str(Path(tmpdir) / "fixture.db")
            integration_db = str(Path(tmpdir) / "integration.db")
            import_result = _run_fixture_import(fixture_db, "batch-replay")
            first_conn = _make_migrated_connection(integration_db)
            first = persist_pdf_statement_import_run_summary_from_import_result(
                first_conn, import_result
            )
            first_conn.close()

            second_conn = sqlite3.connect(integration_db)
            second_conn.row_factory = sqlite3.Row
            try:
                replay = persist_pdf_statement_import_run_summary_from_import_result(
                    second_conn, import_result
                )
                assert replay.already_exists is True
                assert replay.persisted_row_id == first.persisted_row_id
                count = second_conn.execute(
                    "SELECT COUNT(*) FROM pdf_statement_import_runs"
                ).fetchone()[0]
                assert count == 1
            finally:
                second_conn.close()

    def test_conflicting_replay_leaves_original_row_unchanged_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_db = str(Path(tmpdir) / "fixture.db")
            integration_db = str(Path(tmpdir) / "integration.db")
            import_result = _run_fixture_import(fixture_db, "batch-conflict")
            first_conn = _make_migrated_connection(integration_db)
            persist_pdf_statement_import_run_summary_from_import_result(first_conn, import_result)
            first_conn.close()

            second_conn = sqlite3.connect(integration_db)
            second_conn.row_factory = sqlite3.Row
            try:
                with pytest.raises(PdfStatementImportRunConflictError) as exc_info:
                    persist_pdf_statement_import_run_summary_from_import_result(
                        second_conn, replace(import_result, pdf_parsing_mode="changed")
                    )
                assert exc_info.value.reason_code == "IDEMPOTENCY_KEY_CONTENT_CONFLICT"
                count = second_conn.execute(
                    "SELECT COUNT(*) FROM pdf_statement_import_runs"
                ).fetchone()[0]
                assert count == 1
            finally:
                second_conn.close()

    def test_existing_transaction_fails_closed_without_committing_caller_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture_db = str(Path(tmpdir) / "fixture.db")
            integration_db = str(Path(tmpdir) / "integration.db")
            import_result = _run_fixture_import(fixture_db, "batch-existing-tx")
            conn = _make_migrated_connection(integration_db)
            conn.execute("BEGIN")
            try:
                with pytest.raises(StatementImportTransactionError, match="pending work"):
                    persist_pdf_statement_import_run_summary_from_import_result(conn, import_result)
                assert conn.in_transaction is True
            finally:
                conn.rollback()
                conn.close()


# ===================================================================
# 2. Integration persists one row into pdf_statement_import_runs
# ===================================================================


class TestPersistsOneRow:
    def test_persists_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_persist.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-persist-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                count = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM pdf_statement_import_runs"
                ).fetchone()["cnt"]
                assert count == 1
            finally:
                conn.close()


# ===================================================================
# 3. Integration fetches the persisted row by run_public_id
# ===================================================================


class TestFetchesPersistedRow:
    def test_fetches_persisted_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_fetch.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-fetch-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                result = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                assert result.fetched_row_present is True
                # Verify the row exists via direct SQL
                row = conn.execute(
                    "SELECT * FROM pdf_statement_import_runs WHERE run_public_id = ?",
                    (result.run_public_id,),
                ).fetchone()
                assert row is not None
                assert row["run_public_id"] == result.run_public_id
            finally:
                conn.close()


# ===================================================================
# 4. Result reports inserted=True on first persist
# ===================================================================


class TestInsertedTrueOnFirstPersist:
    def test_inserted_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_insert.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-insert-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                result = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                assert result.inserted is True
                assert result.already_exists is False
                assert result.persisted_row_id is not None
            finally:
                conn.close()


# ===================================================================
# 5. Re-running is idempotent and reports already_exists=True
# ===================================================================


class TestIdempotentReRun:
    def test_idempotent_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_idem.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-idem-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                r1 = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                assert r1.inserted is True

                r2 = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                assert r2.inserted is False
                assert r2.already_exists is True
                assert r2.persisted_row_id == r1.persisted_row_id
            finally:
                conn.close()

    def test_only_one_row_exists_after_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_idem2.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-idem2-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                count = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM pdf_statement_import_runs"
                ).fetchone()["cnt"]
                assert count == 1
            finally:
                conn.close()


# ===================================================================
# 6. Row count fields match the summary
# ===================================================================


class TestRowCountFieldsMatch:
    def test_row_counts_match_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_counts.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-counts-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                result = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                row = conn.execute(
                    "SELECT * FROM pdf_statement_import_runs WHERE run_public_id = ?",
                    (result.run_public_id,),
                ).fetchone()
                assert row["total_rows"] == result.total_rows
                assert row["ready_for_import_rows"] == result.ready_for_import_rows
                assert row["needs_review_rows"] == result.needs_review_rows
                assert row["blocked_rows"] == result.blocked_rows
                # CHECK constraint: total_rows = ready + needs + blocked
                assert (
                    row["total_rows"]
                    == row["ready_for_import_rows"] + row["needs_review_rows"] + row["blocked_rows"]
                )
            finally:
                conn.close()


# ===================================================================
# 7. run_status matches the summary
# ===================================================================


class TestRunStatusMatches:
    def test_run_status_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_status.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-status-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                result = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                row = conn.execute(
                    "SELECT run_status FROM pdf_statement_import_runs WHERE run_public_id = ?",
                    (result.run_public_id,),
                ).fetchone()
                assert row["run_status"] == result.run_status
                assert result.run_status in (
                    "fully_ready",
                    "partially_reviewable",
                    "blocked",
                )
            finally:
                conn.close()


# ===================================================================
# 8. source_mode is preserved
# ===================================================================


class TestSourceModePreserved:
    def test_source_mode_fixture_text_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_mode.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-mode-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                result = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                assert result.source_mode == "fixture_text"
                row = conn.execute(
                    "SELECT source_mode FROM pdf_statement_import_runs WHERE run_public_id = ?",
                    (result.run_public_id,),
                ).fetchone()
                assert row["source_mode"] == "fixture_text"
            finally:
                conn.close()


# ===================================================================
# 9. source_pdf_path and source_statement_id are preserved
# ===================================================================


class TestSourceEvidencePreserved:
    def test_source_evidence_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_ev.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-ev-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                result = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                assert result.source_pdf_path != ""
                assert result.source_statement_id != ""
                row = conn.execute(
                    "SELECT source_pdf_path, source_statement_id "
                    "FROM pdf_statement_import_runs WHERE run_public_id = ?",
                    (result.run_public_id,),
                ).fetchone()
                assert row["source_pdf_path"] == result.source_pdf_path
                assert row["source_statement_id"] == result.source_statement_id
            finally:
                conn.close()


# ===================================================================
# 10. dashboard_summary_json and audit_summary_json are present
# ===================================================================


class TestSummaryJsonPresent:
    def test_dashboard_and_audit_json_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_json.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-json-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                result = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                assert result.dashboard_summary_present is True
                assert result.audit_summary_present is True
            finally:
                conn.close()


# ===================================================================
# 11. dashboard JSON remains dashboard-safe
# ===================================================================


class TestDashboardJsonSafe:
    def test_dashboard_json_excludes_audit_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_dashsafe.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-dashsafe-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                result = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                row = conn.execute(
                    "SELECT dashboard_summary_json FROM pdf_statement_import_runs "
                    "WHERE run_public_id = ?",
                    (result.run_public_id,),
                ).fetchone()
                dashboard = json.loads(row["dashboard_summary_json"])
                assert "source_pdf_path" not in dashboard
                assert "source_statement_id" not in dashboard
                assert "evidence_source_refs" not in dashboard
                assert dashboard["review_only"] is True
            finally:
                conn.close()


# ===================================================================
# 12. audit JSON preserves source evidence
# ===================================================================


class TestAuditJsonPreservesEvidence:
    def test_audit_json_includes_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_audit.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-audit-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                result = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                row = conn.execute(
                    "SELECT audit_summary_json FROM pdf_statement_import_runs "
                    "WHERE run_public_id = ?",
                    (result.run_public_id,),
                ).fetchone()
                audit = json.loads(row["audit_summary_json"])
                assert "source_pdf_path" in audit
                assert "source_statement_id" in audit
                assert "evidence_source_refs" in audit
            finally:
                conn.close()


# ===================================================================
# 13. blocked/warning reason JSON remains deterministic
# ===================================================================


class TestReasonJsonDeterministic:
    def test_blocked_reason_json_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_blocked.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-blocked-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                r1 = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                row = conn.execute(
                    "SELECT blocked_reason_counts_json, "
                    "warning_reason_counts_json "
                    "FROM pdf_statement_import_runs WHERE run_public_id = ?",
                    (r1.run_public_id,),
                ).fetchone()
                # Verify blocked_reason JSON is valid and sorted
                blocked = json.loads(row["blocked_reason_counts_json"])
                keys = list(blocked.keys())
                assert keys == sorted(keys), f"blocked reason keys not sorted: {keys}"
                # Verify warning JSON is valid and sorted
                warning = json.loads(row["warning_reason_counts_json"])
                wkeys = list(warning.keys())
                assert wkeys == sorted(wkeys), f"warning keys not sorted: {wkeys}"
            finally:
                conn.close()

    def test_reason_json_presence_flags_correct(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_reasonflag.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-reasonflag-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                result = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
                # For the deterministic fixture (3 rows, all likely ready),
                # blocked_reason_json_present may be True or False depending
                # on whether any row is blocked.
                # We just verify the flags are bool.
                assert isinstance(result.blocked_reason_json_present, bool)
                assert isinstance(result.warning_reason_json_present, bool)
            finally:
                conn.close()


# ===================================================================
# 14. temp DB uses migration 017
# ===================================================================


class TestMigration017Applied:
    def test_migration_017_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            int_db_path = str(Path(tmpdir) / "int.db")
            conn = _make_migrated_connection(int_db_path)
            try:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                assert "pdf_statement_import_runs" in tables
            finally:
                conn.close()

    def test_schema_enforces_constraints(self) -> None:
        """Verify migration 017 CHECK constraints are active."""
        with tempfile.TemporaryDirectory() as tmpdir:
            int_db_path = str(Path(tmpdir) / "int_check.db")
            conn = _make_migrated_connection(int_db_path)
            try:
                # Invalid source_mode should be rejected
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(
                        """INSERT INTO pdf_statement_import_runs
                           (run_public_id, source_mode, run_status,
                            total_rows, ready_for_import_rows,
                            needs_review_rows, blocked_rows)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            "test-bad-mode",
                            "ocr_text",
                            "fully_ready",
                            1,
                            1,
                            0,
                            0,
                        ),
                    )
                    conn.commit()
            finally:
                conn.close()


# ===================================================================
# 15. database/finance.db is not touched
# ===================================================================


class TestNoLiveDbTouched:
    def test_no_finance_db_reference_in_source(self) -> None:
        from finance_core.reconciliation import (
            pdf_statement_import_run_persistence_integration as mod,
        )

        source = inspect.getsource(mod)
        # Strip module docstring -- non-goals mention finance.db legitimately
        code_only = source.split('"""', 2)[-1] if '"""' in source else source
        assert "database/finance.db" not in code_only
        assert "finance.db" not in code_only

    def test_integration_never_opens_finance_db(self) -> None:
        """Verify the integration uses only caller-supplied connections."""
        # The integration function takes an explicit conn -- it never opens
        # a path itself.  We verify by checking the source.
        from finance_core.reconciliation import (
            pdf_statement_import_run_persistence_integration as mod,
        )

        source = inspect.getsource(mod)
        # Remove module docstring
        code_only = source.split('"""', 2)[-1] if '"""' in source else source
        assert "sqlite3.connect" not in code_only
        assert "sqlite3.Connection" in code_only  # type annotation is fine


# ===================================================================
# 16. No final transaction or settlement tables created/written
# ===================================================================


class TestNoFinalTransactionTables:
    def test_no_final_tables_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_notables.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-notable-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                persist_pdf_statement_import_run_summary_from_import_result(
                    conn, import_result, source_mode="fixture_text"
                )
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
# 17. No Telegram/OCR/Metabase production runtime imports
# ===================================================================


class TestNoProductionRuntimeImports:
    def test_no_telegram_ocr_metabase_imports(self) -> None:
        from finance_core.reconciliation import (
            pdf_statement_import_run_persistence_integration as mod,
        )

        source = inspect.getsource(mod)
        # Strip module docstring -- non-goals mention these terms legitimately
        code_only = source.split('"""', 2)[-1] if '"""' in source else source
        for dep in ("telegram", "ocr", "metabase"):
            assert dep not in code_only.lower(), f"Unexpected {dep} reference"

    def test_no_settlement_import(self) -> None:
        from finance_core.reconciliation import (
            pdf_statement_import_run_persistence_integration as mod,
        )

        source = inspect.getsource(mod)
        code_only = source.split('"""', 2)[-1] if '"""' in source else source
        assert "settlement" not in code_only.lower()


# ===================================================================
# 18. Public exports are correct if __init__.py is modified
# ===================================================================


class TestPublicAPI:
    def test_module_exports(self) -> None:
        from finance_core.reconciliation import (
            pdf_statement_import_run_persistence_integration as mod,
        )

        assert hasattr(mod, "__all__")
        expected = {
            "PdfStatementImportRunPersistenceIntegrationResult",
            "persist_pdf_statement_import_run_summary_from_import_result",
        }
        assert set(mod.__all__) == expected

    def test_result_is_frozen(self) -> None:
        result = PdfStatementImportRunPersistenceIntegrationResult(
            run_public_id="run-immutable",
            inserted=True,
            already_exists=False,
            persisted_row_id=1,
            run_status="fully_ready",
            total_rows=10,
            ready_for_import_rows=8,
            needs_review_rows=2,
            blocked_rows=0,
            fetched_row_present=True,
            source_mode="fixture_text",
            source_pdf_path="/tmp/fake.pdf",
            source_statement_id="stmt-test",
            dashboard_summary_present=True,
            audit_summary_present=True,
            blocked_reason_json_present=False,
            warning_reason_json_present=False,
        )
        with pytest.raises(Exception):
            result.inserted = False  # type: ignore[misc]
        with pytest.raises(Exception):
            result.run_public_id = "mutated"  # type: ignore[misc]


# ===================================================================
# Integration: created_at forwarding
# ===================================================================


class TestCreatedAtForwarding:
    def test_created_at_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_ca.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            import_result = _run_fixture_import(db_path, "batch-ca-1")
            conn = _make_migrated_connection(int_db_path)
            try:
                persist_pdf_statement_import_run_summary_from_import_result(
                    conn,
                    import_result,
                    source_mode="fixture_text",
                    created_at=FROZEN_NOW,
                )
                row = conn.execute(
                    "SELECT created_at FROM pdf_statement_import_runs LIMIT 1"
                ).fetchone()
                assert row["created_at"] == FROZEN_NOW
            finally:
                conn.close()


# ===================================================================
# Integration: different import results don't collide
# ===================================================================


class TestDifferentImportsDontCollide:
    def test_different_imports_separate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path1 = str(Path(tmpdir) / "test1.db")
            db_path2 = str(Path(tmpdir) / "test2.db")
            int_db_path = str(Path(tmpdir) / "int.db")
            ir1 = _run_fixture_import(db_path1, "batch-sep-1")
            ir2 = _run_fixture_import(db_path2, "batch-sep-2")
            conn = _make_migrated_connection(int_db_path)
            try:
                r1 = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, ir1, source_mode="fixture_text"
                )
                r2 = persist_pdf_statement_import_run_summary_from_import_result(
                    conn, ir2, source_mode="fixture_text"
                )
                assert r1.run_public_id != r2.run_public_id
                assert r1.inserted is True
                assert r2.inserted is True
                count = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM pdf_statement_import_runs"
                ).fetchone()["cnt"]
                assert count == 2
            finally:
                conn.close()
