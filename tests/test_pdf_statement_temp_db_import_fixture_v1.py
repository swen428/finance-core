"""Tests for PDF Statement Temp DB Import Fixture v1."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from finance_core.reconciliation.migrations import LIVE_DB_PATH
from finance_core.reconciliation.pdf_statement_temp_db_import_fixture import (
    DEFAULT_BATCH_PUBLIC_ID,
    DEFAULT_PDF_FIXTURE_PATH,
    DEFAULT_TEMPLATE_ID,
    DEFAULT_TEXT_FIXTURE_PATH,
    assert_safe_temp_db_path,
    build_parse_result_from_text_fixture,
    import_pdf_statement_fixture_to_temp_db,
    main,
    result_to_summary_dict,
)


def test_text_fixture_parse_preserves_pdf_source_path() -> None:
    result = build_parse_result_from_text_fixture()

    assert result.pdf_path == str(DEFAULT_PDF_FIXTURE_PATH.resolve())
    assert result.template_id == DEFAULT_TEMPLATE_ID
    assert result.total_rows == 3
    assert result.ok_count == 3
    assert result.extraction_success is True
    assert result.extraction_warnings == ("fixture_text_used_instead_of_direct_pdf_extraction",)
    assert {row.source_path for row in result.rows} == {str(DEFAULT_PDF_FIXTURE_PATH.resolve())}


def test_import_fixture_writes_normalized_rows_to_temp_db(tmp_path: Path) -> None:
    db_path = tmp_path / "pdf_statement_fixture.sqlite"
    result = import_pdf_statement_fixture_to_temp_db(db_path=db_path)

    assert result.db_path == str(db_path.resolve())
    assert result.pdf_path == str(DEFAULT_PDF_FIXTURE_PATH.resolve())
    assert result.text_fixture_path == str(DEFAULT_TEXT_FIXTURE_PATH.resolve())
    assert result.review_queue.summary.ready_for_import_count == 3
    assert result.review_queue.summary.blocked_count == 0
    assert result.normalization_result.accepted_count == 3
    assert result.import_batch.row_count == 3
    assert len(result.import_batch.inserted_ids) == 3
    assert result.import_batch.idempotent_count == 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
        assert Path(db_file).resolve() == db_path.resolve()
        assert Path(db_file).resolve() != LIVE_DB_PATH.resolve()

        batch = conn.execute(
            "SELECT * FROM statement_import_batches WHERE public_id = ?",
            (DEFAULT_BATCH_PUBLIC_ID,),
        ).fetchone()
        assert batch["source_type"] == "bank_statement"
        assert batch["source_file_path"] == str(DEFAULT_PDF_FIXTURE_PATH.resolve())
        assert batch["currency"] == "MYR"

        rows = conn.execute(
            """
            SELECT merchant_raw, amount, currency, transaction_date,
                   amount_direction, statement_row_reference,
                   raw_row_payload_json, row_fingerprint
            FROM statement_transactions
            ORDER BY statement_row_reference
            """
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 3
    assert [row["merchant_raw"] for row in rows] == ["CoffeeShop", "Salary", "Grocer"]
    assert [row["amount"] for row in rows] == [12.5, 1000, 45.67]
    assert {row["currency"] for row in rows} == {"MYR"}
    assert [row["transaction_date"] for row in rows] == [
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
    ]
    assert [row["amount_direction"] for row in rows] == ["debit", "credit", "debit"]
    assert [row["statement_row_reference"] for row in rows] == [
        "page-1:line-1",
        "page-1:line-2",
        "page-1:line-3",
    ]
    assert all(len(str(row["row_fingerprint"])) == 64 for row in rows)
    assert all(int(str(row["row_fingerprint"]), 16) >= 0 for row in rows)

    first_payload = json.loads(rows[0]["raw_row_payload_json"])
    assert first_payload["attachment_path"] == str(DEFAULT_PDF_FIXTURE_PATH.resolve())
    assert first_payload["raw_row_text"] == "01/07/2026 CoffeeShop 12.50 D"


def test_duplicate_pdf_fixture_import_does_not_create_duplicate_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "pdf_statement_fixture.sqlite"

    first = import_pdf_statement_fixture_to_temp_db(db_path=db_path)
    second = import_pdf_statement_fixture_to_temp_db(db_path=db_path)

    assert len(first.import_batch.inserted_ids) == 3
    assert len(second.import_batch.inserted_ids) == 0
    assert second.import_batch.idempotent_count == 3
    assert second.import_batch.skipped_duplicates == 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) AS cnt FROM statement_transactions").fetchone()
    finally:
        conn.close()
    assert total["cnt"] == 3


def test_fixture_refuses_live_database_path() -> None:
    with pytest.raises(ValueError, match="Refusing to use database/finance.db"):
        assert_safe_temp_db_path(LIVE_DB_PATH)

    with pytest.raises(ValueError, match="Refusing to use database/finance.db"):
        import_pdf_statement_fixture_to_temp_db(db_path=LIVE_DB_PATH)


def test_summary_dict_declares_fixture_text_pdf_mode(tmp_path: Path) -> None:
    result = import_pdf_statement_fixture_to_temp_db(db_path=tmp_path / "fixture.sqlite")
    summary = result_to_summary_dict(result)

    assert summary["pdf_parsing_mode"] == "fixture_text"
    assert summary["pdf_path"] == str(DEFAULT_PDF_FIXTURE_PATH.resolve())
    assert summary["text_fixture_path"] == str(DEFAULT_TEXT_FIXTURE_PATH.resolve())
    assert summary["inserted_rows"] == 3
    assert summary["review_only"] is True
    assert summary["not_final_financial_record"] is True


def test_cli_imports_fixture_to_temp_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "cli_fixture.sqlite"

    exit_code = main(["--db", str(db_path), "--json"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["db_path"] == str(db_path.resolve())
    assert output["inserted_rows"] == 3
    assert output["pdf_parsing_mode"] == "fixture_text"
    assert db_path.exists()
