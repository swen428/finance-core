"""Tests for PDF Statement Import Run Reporting Query Pack v1.

Covers dashboard row shape, audit/reporting separation, deterministic
ordering, empty-result behaviour, aggregation helpers, and safety
guarantees.  Every test uses temporary SQLite databases only -- never
touches database/finance.db or live data.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from finance_core.reconciliation.pdf_statement_import_run_reporting import (
    PdfImportRunDashboardRow,
    PdfImportRunReportingQueryPack,
    count_pdf_import_runs_by_source_mode,
    count_pdf_import_runs_by_status,
    list_pdf_import_run_dashboard_rows,
    list_pdf_import_run_dashboard_rows_as_dicts,
)
from finance_core.resources import migrations_dir

MIGRATION_PATH = migrations_dir() / "017_pdf_statement_import_run_persistence.sql"

FROZEN_NOW = "2026-07-09T12:00:00+08:00"


# -- helpers --


def _apply_migration_017(conn: sqlite3.Connection) -> None:
    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    conn.executescript(migration_sql)


def _make_migrated_temp_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_migration_017(conn)
    return conn


def _insert_sample_run(
    conn: sqlite3.Connection,
    *,
    run_public_id: str,
    import_batch_public_id: str | None = "batch_test",
    source_mode: str = "fixture_text",
    run_status: str = "fully_ready",
    total_rows: int = 10,
    ready_for_import_rows: int = 8,
    needs_review_rows: int = 2,
    blocked_rows: int = 0,
    created_at: str = FROZEN_NOW,
) -> None:
    conn.execute(
        """\
        INSERT INTO pdf_statement_import_runs (
            run_public_id, import_batch_public_id, source_mode,
            run_status, total_rows, ready_for_import_rows,
            needs_review_rows, blocked_rows, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_public_id,
            import_batch_public_id,
            source_mode,
            run_status,
            total_rows,
            ready_for_import_rows,
            needs_review_rows,
            blocked_rows,
            created_at,
        ),
    )
    conn.commit()


# ===================================================================
# 1. Dashboard row dataclass
# ===================================================================


class TestDashboardRowDataclass:
    def test_row_is_frozen(self) -> None:
        row = PdfImportRunDashboardRow(
            run_public_id="run-001",
            import_batch_public_id="batch-001",
            source_mode="fixture_text",
            run_status="fully_ready",
            total_rows=10,
            ready_for_import_rows=8,
            needs_review_rows=2,
            blocked_rows=0,
            created_at=FROZEN_NOW,
        )
        with pytest.raises(Exception):
            row.run_public_id = "mutated"  # type: ignore[misc]

    def test_to_dict_has_dashboard_keys_only(self) -> None:
        row = PdfImportRunDashboardRow(
            run_public_id="run-001",
            import_batch_public_id="batch-001",
            source_mode="fixture_text",
            run_status="fully_ready",
            total_rows=10,
            ready_for_import_rows=8,
            needs_review_rows=2,
            blocked_rows=0,
            created_at=FROZEN_NOW,
        )
        d = row.to_dict()
        assert set(d.keys()) == set(PdfImportRunReportingQueryPack.DASHBOARD_FIELD_NAMES)
        # No audit fields
        for audit_field in PdfImportRunReportingQueryPack.AUDIT_ONLY_FIELD_NAMES:
            assert audit_field not in d


# ===================================================================
# 2. ClassVar constants
# ===================================================================


class TestClassVarConstants:
    def test_dashboard_fields_are_names_not_audit(self) -> None:
        dashboard = set(PdfImportRunReportingQueryPack.DASHBOARD_FIELD_NAMES)
        audit = set(PdfImportRunReportingQueryPack.AUDIT_ONLY_FIELD_NAMES)
        assert dashboard.isdisjoint(audit)

    def test_audit_fields_exist_in_schema(self) -> None:
        """The AUDIT_ONLY_FIELD_NAMES should match actual schema columns."""
        sql = MIGRATION_PATH.read_text(encoding="utf-8")
        for field in PdfImportRunReportingQueryPack.AUDIT_ONLY_FIELD_NAMES:
            assert field in sql, f"Audit field {field!r} not found in migration SQL"

    def test_dashboard_sql_is_read_only(self) -> None:
        sql = PdfImportRunReportingQueryPack.DASHBOARD_SELECT_SQL.upper()
        assert sql.strip().startswith("SELECT")
        assert "INSERT" not in sql
        assert "UPDATE" not in sql
        assert "DELETE" not in sql
        assert "DROP" not in sql
        # created_at contains "CREATE" as a substring -- check tokens, not raw SQL
        tokens = sql.replace(",", " ").replace("(", " ").replace(")", " ").split()
        assert "DROP" not in tokens
        assert "ALTER" not in sql

    def test_reporting_only_flag(self) -> None:
        assert PdfImportRunReportingQueryPack._reporting_only is True


# ===================================================================
# 3. list_dashboard_rows
# ===================================================================


class TestListDashboardRows:
    def test_returns_correct_shape(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_shape.db")
        conn = _make_migrated_temp_db(db_path)
        _insert_sample_run(conn, run_public_id="run-shape-1")
        conn.close()

        rows = PdfImportRunReportingQueryPack.list_dashboard_rows(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, PdfImportRunDashboardRow)
        assert row.run_public_id == "run-shape-1"
        assert row.source_mode == "fixture_text"
        assert row.run_status == "fully_ready"
        assert row.total_rows == 10
        assert row.ready_for_import_rows == 8
        assert row.needs_review_rows == 2
        assert row.blocked_rows == 0

    def test_returns_empty_list_for_empty_db(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_empty.db")
        _make_migrated_temp_db(db_path).close()
        rows = PdfImportRunReportingQueryPack.list_dashboard_rows(db_path)
        assert rows == []

    def test_ordering_newest_first(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_order.db")
        conn = _make_migrated_temp_db(db_path)
        _insert_sample_run(
            conn,
            run_public_id="run-older",
            created_at="2026-07-08T12:00:00+08:00",
        )
        _insert_sample_run(
            conn,
            run_public_id="run-newer",
            created_at="2026-07-09T12:00:00+08:00",
        )
        conn.close()

        rows = PdfImportRunReportingQueryPack.list_dashboard_rows(db_path)
        assert len(rows) == 2
        assert rows[0].run_public_id == "run-newer"
        assert rows[1].run_public_id == "run-older"

    def test_tie_breaker_by_public_id(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_tie.db")
        conn = _make_migrated_temp_db(db_path)
        _insert_sample_run(conn, run_public_id="run-B")
        _insert_sample_run(conn, run_public_id="run-A")
        conn.close()

        rows = PdfImportRunReportingQueryPack.list_dashboard_rows(db_path)
        assert len(rows) == 2
        assert rows[0].run_public_id == "run-A"
        assert rows[1].run_public_id == "run-B"


# ===================================================================
# 4. list_dashboard_rows_as_dicts
# ===================================================================


class TestListDashboardRowsAsDicts:
    def test_returns_correct_shape(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_dict.db")
        conn = _make_migrated_temp_db(db_path)
        _insert_sample_run(conn, run_public_id="run-dict-1")
        conn.close()

        dicts = PdfImportRunReportingQueryPack.list_dashboard_rows_as_dicts(db_path)
        assert len(dicts) == 1
        d = dicts[0]
        assert isinstance(d, dict)
        assert d["run_public_id"] == "run-dict-1"
        for key in PdfImportRunReportingQueryPack.DASHBOARD_FIELD_NAMES:
            assert key in d
        for audit_field in PdfImportRunReportingQueryPack.AUDIT_ONLY_FIELD_NAMES:
            assert audit_field not in d

    def test_empty_db_returns_empty_list(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_empty_dict.db")
        _make_migrated_temp_db(db_path).close()
        dicts = PdfImportRunReportingQueryPack.list_dashboard_rows_as_dicts(db_path)
        assert dicts == []


# ===================================================================
# 5. count_by_status
# ===================================================================


class TestCountByStatus:
    def test_counts_by_status(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_status.db")
        conn = _make_migrated_temp_db(db_path)
        _insert_sample_run(conn, run_public_id="run-full", run_status="fully_ready")
        _insert_sample_run(conn, run_public_id="run-partial", run_status="partially_reviewable")
        _insert_sample_run(conn, run_public_id="run-blocked", run_status="blocked")
        _insert_sample_run(conn, run_public_id="run-full2", run_status="fully_ready")
        conn.close()

        counts = PdfImportRunReportingQueryPack.count_by_status(db_path)
        assert counts == {"blocked": 1, "fully_ready": 2, "partially_reviewable": 1}

    def test_empty_db_returns_empty_dict(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_empty_status.db")
        _make_migrated_temp_db(db_path).close()
        assert PdfImportRunReportingQueryPack.count_by_status(db_path) == {}


# ===================================================================
# 6. count_by_source_mode
# ===================================================================


class TestCountBySourceMode:
    def test_counts_by_source_mode(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_mode.db")
        conn = _make_migrated_temp_db(db_path)
        _insert_sample_run(conn, run_public_id="run-fix", source_mode="fixture_text")
        _insert_sample_run(conn, run_public_id="run-pdf", source_mode="pdf_text")
        _insert_sample_run(conn, run_public_id="run-fix2", source_mode="fixture_text")
        conn.close()

        counts = PdfImportRunReportingQueryPack.count_by_source_mode(db_path)
        assert counts == {"fixture_text": 2, "pdf_text": 1}

    def test_empty_db_returns_empty_dict(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_empty_mode.db")
        _make_migrated_temp_db(db_path).close()
        assert PdfImportRunReportingQueryPack.count_by_source_mode(db_path) == {}


# ===================================================================
# 7. Convenience functions
# ===================================================================


class TestConvenienceFunctions:
    def test_list_rows_convenience(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_conv.db")
        conn = _make_migrated_temp_db(db_path)
        _insert_sample_run(conn, run_public_id="run-conv")
        conn.close()

        rows = list_pdf_import_run_dashboard_rows(db_path)
        assert len(rows) == 1
        assert rows[0].run_public_id == "run-conv"

    def test_list_dicts_convenience(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_conv2.db")
        conn = _make_migrated_temp_db(db_path)
        _insert_sample_run(conn, run_public_id="run-conv2")
        conn.close()

        dicts = list_pdf_import_run_dashboard_rows_as_dicts(db_path)
        assert len(dicts) == 1
        assert dicts[0]["run_public_id"] == "run-conv2"

    def test_count_status_convenience(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_conv3.db")
        conn = _make_migrated_temp_db(db_path)
        _insert_sample_run(conn, run_public_id="run-conv3", run_status="fully_ready")
        conn.close()

        counts = count_pdf_import_runs_by_status(db_path)
        assert counts == {"fully_ready": 1}

    def test_count_mode_convenience(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_conv4.db")
        conn = _make_migrated_temp_db(db_path)
        _insert_sample_run(conn, run_public_id="run-conv4", source_mode="fixture_text")
        conn.close()

        counts = count_pdf_import_runs_by_source_mode(db_path)
        assert counts == {"fixture_text": 1}


# ===================================================================
# 8. No database/finance.db touched
# ===================================================================


class TestNoLiveDbTouched:
    def test_module_never_references_finance_db(self) -> None:
        from finance_core.reconciliation import (
            pdf_statement_import_run_reporting as mod,
        )

        source = inspect.getsource(mod)
        # Strip module docstring (non-goals mention finance.db)
        code_only = source.split('"""', 2)[-1] if '"""' in source else source
        assert "database/finance.db" not in code_only
        assert "finance.db" not in code_only

    def test_accepts_explicit_path_only(self) -> None:
        """Verify the functions accept a db_path parameter and never
        hard-code a path."""
        for func in (
            "list_dashboard_rows",
            "list_dashboard_rows_as_dicts",
            "count_by_status",
            "count_by_source_mode",
        ):
            sig = inspect.signature(getattr(PdfImportRunReportingQueryPack, func))
            assert "db_path" in sig.parameters


# ===================================================================
# 9. No final transaction / settlement writes
# ===================================================================


class TestNoFinalTransactionWrites:
    def test_no_insert_in_source(self) -> None:
        from finance_core.reconciliation import (
            pdf_statement_import_run_reporting as mod,
        )

        source = inspect.getsource(mod)
        code_only = source.split('"""', 2)[-1] if '"""' in source else source
        assert "INSERT " not in code_only
        assert "UPDATE " not in code_only
        assert "DELETE " not in code_only


# ===================================================================
# 10. No Telegram / OCR / Metabase runtime imports
# ===================================================================


class TestNoProductionRuntimeImports:
    def test_no_telegram_ocr_metabase_imports(self) -> None:
        from finance_core.reconciliation import (
            pdf_statement_import_run_reporting as mod,
        )

        source = inspect.getsource(mod)
        code_only = source.split('"""', 2)[-1] if '"""' in source else source
        for dep in ("telegram", "ocr", "metabase"):
            assert dep not in code_only.lower(), f"Unexpected {dep} reference"

    def test_no_settlement_import(self) -> None:
        from finance_core.reconciliation import (
            pdf_statement_import_run_reporting as mod,
        )

        source = inspect.getsource(mod)
        code_only = source.split('"""', 2)[-1] if '"""' in source else source
        assert "settlement" not in code_only.lower()


# ===================================================================
# 11. Module __all__ is correct
# ===================================================================


class TestPublicAPI:
    def test_all_exports(self) -> None:
        from finance_core.reconciliation import (
            pdf_statement_import_run_reporting as mod,
        )

        assert hasattr(mod, "__all__")
        expected = {
            "PdfImportRunDashboardRow",
            "PdfImportRunReportingQueryPack",
            "list_pdf_import_run_dashboard_rows",
            "list_pdf_import_run_dashboard_rows_as_dicts",
            "count_pdf_import_runs_by_status",
            "count_pdf_import_runs_by_source_mode",
        }
        assert set(mod.__all__) == expected


# ===================================================================
# 12. Migration 017 compatibility
# ===================================================================


class TestMigration017Compatibility:
    def test_queries_work_on_migration_017_table(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_mig17.db")
        conn = _make_migrated_temp_db(db_path)
        _insert_sample_run(conn, run_public_id="run-mig17")
        conn.close()

        rows = PdfImportRunReportingQueryPack.list_dashboard_rows(db_path)
        assert len(rows) == 1

    def test_nullable_import_batch_handled(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test_null_batch.db")
        conn = _make_migrated_temp_db(db_path)
        _insert_sample_run(
            conn,
            run_public_id="run-null-batch",
            import_batch_public_id=None,
        )
        conn.close()

        rows = PdfImportRunReportingQueryPack.list_dashboard_rows(db_path)
        assert rows[0].import_batch_public_id is None
        d = rows[0].to_dict()
        assert d["import_batch_public_id"] is None
