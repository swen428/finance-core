"""Tests for Statement Import Fingerprint Dedup v1.

Covers:
  1. Migration 009 applies cleanly to a temporary test DB
  2. Persisted statement rows include row_fingerprint
  3. First import inserts rows normally
  4. Re-import of the same CSV/source does not create duplicates (public_id dedup)
  5. Duplicate import result reports idempotent_count
  6. Same row content produces the same row fingerprint across repeated imports
  7. Different row content produces a different row fingerprint
  8. Same row fingerprint within same batch does not insert twice
  9. Raw row payload remains preserved
 10. Original statement_row_reference remains preserved
 11. Transaction date and posted date remain separate
 12. Invalid rows from hardened parser are not persisted
 13. database/finance.db is not touched
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from finance_core.reconciliation.statement_csv import (
    StatementCsvAdapter,
    _csv_row_fingerprint,
)
from finance_core.reconciliation.statement_import import (
    StatementImporter,
    StructuredStatementRow,
)

if TYPE_CHECKING:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"


def _fp(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(
    merchant: str = "Apple",
    amount: Decimal = Decimal("10.00"),
    currency: str = "SGD",
    txn_date: date | None = date(2024, 12, 1),
    posted_date: date | None = date(2024, 12, 3),
    **kw,
) -> StructuredStatementRow:
    return StructuredStatementRow(
        merchant_raw=merchant,
        amount=amount,
        currency=currency,
        transaction_date=txn_date,
        posted_date=posted_date,
        **kw,
    )


def _count_rows_in_batch(conn: "sqlite3.Connection", batch_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM statement_transactions WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    return row["cnt"]


# ---------------------------------------------------------------------------
# 1. Migration 009 applies cleanly
# ---------------------------------------------------------------------------


def test_migration_009_applies_cleanly(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Verify migration 009 adds row_fingerprint column and unique index."""
    conn = migrated_temp_db_connection
    cols = conn.execute("PRAGMA table_info(statement_transactions)").fetchall()
    col_names = {c["name"] for c in cols}
    assert "row_fingerprint" in col_names, "row_fingerprint column missing after migration 009"

    indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='statement_transactions'"
    ).fetchall()
    index_names = {i["name"] for i in indexes}
    assert "idx_stmt_txns_batch_fingerprint" in index_names, (
        "idx_stmt_txns_batch_fingerprint index missing after migration 009"
    )


# ---------------------------------------------------------------------------
# 2. Row fingerprint is persisted
# ---------------------------------------------------------------------------


def test_persisted_row_includes_fingerprint(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Insert a row with a fingerprint and verify it is stored."""
    conn = migrated_temp_db_connection
    importer = StatementImporter(conn)
    fingerprint = _fp("csv-abc123def4567890")
    row = _make_row(merchant="Spotify", row_fingerprint=fingerprint)
    batch = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="test-fp-persist",
    )

    stored = conn.execute(
        "SELECT row_fingerprint, external_row_fingerprint "
        "FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stored is not None
    assert stored["row_fingerprint"] != fingerprint
    assert stored["external_row_fingerprint"] == fingerprint


def test_row_without_fingerprint_inserts_normally(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Rows without a supplied fingerprint receive a canonical fingerprint."""
    conn = migrated_temp_db_connection
    importer = StatementImporter(conn)
    row = _make_row(merchant="NoFingerprint")
    batch = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="test-no-fp",
    )
    assert len(batch.inserted_ids) == 1

    stored = conn.execute(
        "SELECT row_fingerprint FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stored["row_fingerprint"] is not None
    assert len(stored["row_fingerprint"]) == 64


# ---------------------------------------------------------------------------
# 3. First import inserts normally
# ---------------------------------------------------------------------------


def test_first_import_inserts_all_rows(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """First import of rows inserts all of them."""
    conn = migrated_temp_db_connection
    importer = StatementImporter(conn)
    rows = [
        _make_row(merchant="Apple", amount=Decimal("10.00"), row_fingerprint=_fp("csv-fp001")),
        _make_row(merchant="Google", amount=Decimal("20.00"), row_fingerprint=_fp("csv-fp002")),
        _make_row(merchant="Netflix", amount=Decimal("15.00"), row_fingerprint=_fp("csv-fp003")),
    ]
    batch = importer.import_rows(
        rows,
        source_type="credit_card_statement",
        public_id="test-first-import",
    )

    assert batch.row_count == 3
    assert len(batch.inserted_ids) == 3
    assert batch.skipped_duplicates == 0
    assert batch.idempotent_count == 0


# ---------------------------------------------------------------------------
# 4. Re-import does not create duplicates (public_id dedup)
# ---------------------------------------------------------------------------


def test_reimport_same_source_no_duplicates(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Re-import of same source rows should not create duplicate rows."""
    conn = migrated_temp_db_connection
    importer = StatementImporter(conn)
    rows = [
        _make_row(merchant="Apple", amount=Decimal("10.00"), row_fingerprint=_fp("csv-r1")),
        _make_row(merchant="Google", amount=Decimal("20.00"), row_fingerprint=_fp("csv-r2")),
    ]
    source_hash = _fp("sha256-reimport-test-001")

    batch1 = importer.import_rows(
        rows,
        source_type="bank_statement",
        public_id="test-reimport-1",
        source_file_hash=source_hash,
    )
    assert len(batch1.inserted_ids) == 2
    assert batch1.skipped_duplicates == 0
    assert batch1.idempotent_count == 0

    # Re-import same source file hash -> same public_ids -> public_id dedup
    batch2 = importer.import_rows(
        rows,
        source_type="bank_statement",
        public_id="test-reimport-2",
        source_file_hash=source_hash,
    )
    assert len(batch2.inserted_ids) == 0
    assert batch2.skipped_duplicates == 0
    assert batch2.idempotent_count == 2
    assert batch2.batch_id == batch1.batch_id

    # Only 2 rows should exist across both batches
    total = conn.execute("SELECT COUNT(*) as cnt FROM statement_transactions").fetchone()
    assert total["cnt"] == 2


# ---------------------------------------------------------------------------
# 5. Duplicate import reports idempotent_count
# ---------------------------------------------------------------------------


def test_fingerprint_idempotent_count(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Same fingerprint within a batch increments idempotent_count."""
    conn = migrated_temp_db_connection
    importer = StatementImporter(conn)
    rows = [
        _make_row(merchant="Shopee", amount=Decimal("50.00"), row_fingerprint=_fp("csv-fp-50")),
        _make_row(merchant="Lazada", amount=Decimal("30.00"), row_fingerprint=_fp("csv-fp-30")),
    ]

    # First import: both rows insert normally
    batch1 = importer.import_rows(
        rows,
        source_type="bank_statement",
        public_id="test-idempotent",
    )
    assert len(batch1.inserted_ids) == 2
    assert batch1.skipped_duplicates == 0
    assert batch1.idempotent_count == 0
    assert batch1.row_count == 2

    # Re-import same rows, same batch_public_id -> batch is reused (same batch_id).
    # Both fingerprints already exist in that batch, so idempotent_count
    # increments for each row and no new inserts are attempted.
    # This proves fingerprint-level dedup, not public_id dedup, because the
    # fingerprint check happens first in the import loop and both are satisfied
    # before any insert or DuplicatePublicIdError can fire.
    batch2 = importer.import_rows(
        rows,
        source_type="bank_statement",
        public_id="test-idempotent",
    )
    assert len(batch2.inserted_ids) == 0
    assert batch2.skipped_duplicates == 0
    assert batch2.idempotent_count == 2
    assert _count_rows_in_batch(conn, batch2.batch_id) == 2
    total = conn.execute("SELECT COUNT(*) as cnt FROM statement_transactions").fetchone()
    assert total["cnt"] == 2


def test_import_result_has_idempotent_field(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """StatementImportBatch exposes idempotent_count field (default 0)."""
    conn = migrated_temp_db_connection
    importer = StatementImporter(conn)
    rows = [_make_row(merchant="Test", row_fingerprint=_fp("csv-fp-test"))]
    batch = importer.import_rows(
        rows,
        source_type="bank_statement",
        public_id="test-has-idempotent",
    )
    assert hasattr(batch, "idempotent_count")
    assert batch.idempotent_count == 0


# ---------------------------------------------------------------------------
# 6. Same row content -> same fingerprint
# ---------------------------------------------------------------------------


def test_same_content_same_fingerprint() -> None:
    """Identical raw row content produces identical fingerprints."""
    row_a = {
        "transaction_date": "2024-01-01",
        "merchant_raw": "Apple",
        "amount": "10.00",
        "currency": "SGD",
    }
    row_b = {
        "transaction_date": "2024-01-01",
        "merchant_raw": "Apple",
        "amount": "10.00",
        "currency": "SGD",
    }
    fp_a = _csv_row_fingerprint(row_a)
    fp_b = _csv_row_fingerprint(row_b)
    assert fp_a == fp_b, f"Same content must produce same fingerprint: {fp_a} != {fp_b}"


# ---------------------------------------------------------------------------
# 7. Different row content -> different fingerprint
# ---------------------------------------------------------------------------


def test_different_content_different_fingerprint() -> None:
    """Different raw row content produces different fingerprints."""
    row_a = {
        "transaction_date": "2024-01-01",
        "merchant_raw": "Apple",
        "amount": "10.00",
        "currency": "SGD",
    }
    row_b = {
        "transaction_date": "2024-01-02",
        "merchant_raw": "Apple",
        "amount": "10.00",
        "currency": "SGD",
    }
    fp_a = _csv_row_fingerprint(row_a)
    fp_b = _csv_row_fingerprint(row_b)
    assert fp_a != fp_b, "Different content must produce different fingerprints"


def test_fingerprint_format() -> None:
    """Row fingerprint is a full lowercase SHA-256 digest."""
    row = {"merchant_raw": "Test", "amount": "5.00", "currency": "SGD"}
    fp = _csv_row_fingerprint(row)
    assert len(fp) == 64
    assert fp == fp.lower()
    int(fp, 16)


# ---------------------------------------------------------------------------
# 8. Same-batch fingerprint dedup within a single import call
# ---------------------------------------------------------------------------


def test_same_external_fingerprint_cannot_hide_changed_canonical_material(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """A supplied fingerprint cannot hide changed material row evidence."""
    conn = migrated_temp_db_connection
    importer = StatementImporter(conn)

    duplicate_fingerprint = _fp("csv-same-fingerprint")
    rows = [
        _make_row(
            merchant="Duplicate Merchant",
            amount=Decimal("10.00"),
            statement_row_reference="BANK-REF-001",
            row_fingerprint=duplicate_fingerprint,
        ),
        _make_row(
            merchant="Duplicate Merchant",
            amount=Decimal("10.00"),
            statement_row_reference="BANK-REF-002",
            row_fingerprint=duplicate_fingerprint,
        ),
    ]

    batch = importer.import_rows(
        rows,
        source_type="bank_statement",
        public_id="test-same-external-fingerprint",
    )
    persisted = conn.execute(
        "SELECT row_fingerprint, external_row_fingerprint "
        "FROM statement_transactions WHERE batch_id = ? ORDER BY id",
        (batch.batch_id,),
    ).fetchall()
    assert len({row["row_fingerprint"] for row in persisted}) == 2
    assert {row["external_row_fingerprint"] for row in persisted} == {duplicate_fingerprint}

    replay = importer.import_rows(
        list(reversed(rows)),
        source_type="bank_statement",
        public_id="test-same-external-fingerprint",
    )
    assert replay.batch_id == batch.batch_id
    assert replay.inserted_ids == []
    assert replay.idempotent_count == 2


# ---------------------------------------------------------------------------
# 9. Raw row payload preserved
# ---------------------------------------------------------------------------


def test_raw_row_payload_preserved_with_fingerprint(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """raw_row_payload_json is preserved when row_fingerprint is present."""
    conn = migrated_temp_db_connection
    importer = StatementImporter(conn)
    payload = {"original_csv": "data", "line": 5}
    row = _make_row(
        merchant="Preserved",
        raw_row_payload=payload,
        row_fingerprint=_fp("csv-fp-preserved"),
    )
    batch = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="test-payload",
    )

    stored = conn.execute(
        "SELECT raw_row_payload_json FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stored is not None
    parsed = json.loads(stored["raw_row_payload_json"])
    assert parsed["original_csv"] == "data"
    assert parsed["line"] == 5


# ---------------------------------------------------------------------------
# 10. statement_row_reference preserved
# ---------------------------------------------------------------------------


def test_statement_row_reference_preserved_with_fingerprint(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """statement_row_reference is preserved alongside row_fingerprint."""
    conn = migrated_temp_db_connection
    importer = StatementImporter(conn)
    row = _make_row(
        merchant="RefTest",
        statement_row_reference="BANK-REF-001",
        row_fingerprint=_fp("csv-fp-ref"),
    )
    batch = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="test-ref",
    )

    stored = conn.execute(
        "SELECT statement_row_reference, row_fingerprint, external_row_fingerprint "
        "FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stored is not None
    assert stored["statement_row_reference"] == "BANK-REF-001"
    assert stored["row_fingerprint"] != _fp("csv-fp-ref")
    assert stored["external_row_fingerprint"] == _fp("csv-fp-ref")


# ---------------------------------------------------------------------------
# 11. Transaction date and posted date preserved separately
# ---------------------------------------------------------------------------


def test_dates_preserved_with_fingerprint(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Transaction and posted dates are stored separately alongside fingerprint."""
    conn = migrated_temp_db_connection
    importer = StatementImporter(conn)
    row = _make_row(
        merchant="DateTest",
        txn_date=date(2024, 12, 5),
        posted_date=date(2024, 12, 8),
        row_fingerprint=_fp("csv-fp-date"),
    )
    batch = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="test-dates",
    )

    stored = conn.execute(
        "SELECT transaction_date, posted_date, row_fingerprint, external_row_fingerprint "
        "FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stored is not None
    assert stored["transaction_date"] == "2024-12-05"
    assert stored["posted_date"] == "2024-12-08"
    assert stored["row_fingerprint"] != _fp("csv-fp-date")
    assert stored["external_row_fingerprint"] == _fp("csv-fp-date")


# ---------------------------------------------------------------------------
# 12. Hardened CSV parser defers canonical fingerprint ownership
# ---------------------------------------------------------------------------


def test_hardened_csv_parser_defers_canonical_fingerprint_to_importer() -> None:
    """The parser preserves source identity without mislabeling parser-local material."""
    csv_text = (
        "transaction_date,merchant_raw,amount,currency,reference\n"
        "2024-01-15,Spotify,12.99,SGD,SPOT-001\n"
    )
    adapter = StatementCsvAdapter()
    result = adapter.parse_text_hardened(csv_text, source="test-csv")
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.row_fingerprint is None
    assert row.row_fingerprint_version is None
    assert row.fingerprint_source_content_hash == result.source_content_hash


# ---------------------------------------------------------------------------
# 13. Cross-batch dedup preserves rows from different sources
# ---------------------------------------------------------------------------


def test_different_sources_not_collapsed(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Rows with same content but different source_file_hash should not dedup."""
    conn = migrated_temp_db_connection
    importer = StatementImporter(conn)
    row = _make_row(
        merchant="Common merchant",
        amount=Decimal("25.00"),
        row_fingerprint=_fp("csv-fp-common"),
    )

    batch1 = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="test-bank-a",
        source_file_hash=_fp("hash-bank-a"),
    )
    assert len(batch1.inserted_ids) == 1
    assert batch1.skipped_duplicates == 0
    assert batch1.idempotent_count == 0

    batch2 = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="test-bank-b",
        source_file_hash=_fp("hash-bank-b"),
    )
    # Different source_file_hash -> different public_id -> should NOT dedup
    assert len(batch2.inserted_ids) == 1, (
        "Rows from different source files should not be deduped by public_id"
    )
    assert batch2.skipped_duplicates == 0
    assert batch2.idempotent_count == 0

    total = conn.execute("SELECT COUNT(*) as cnt FROM statement_transactions").fetchone()
    assert total["cnt"] == 2


# ---------------------------------------------------------------------------
# 14. Live DB guard
# ---------------------------------------------------------------------------


def test_live_db_not_touched(migrated_temp_db_connection: "sqlite3.Connection") -> None:
    """Verify tests only touch temporary databases, not finance.db."""
    conn = migrated_temp_db_connection
    db_path = conn.execute("PRAGMA database_list").fetchone()
    # The temp DB path should be under a temp directory, not the live path
    db_file = db_path[2] if db_path else ""
    assert LIVE_DB_PATH.as_posix() not in str(db_file), "Test is using live database finance.db!"


# ---------------------------------------------------------------------------
# 15. Non-CSV StructuredStatementRow without fingerprint works
# ---------------------------------------------------------------------------


def test_importer_handles_non_csv_row_without_fingerprint(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Rows from non-CSV sources (no fingerprint) insert without error."""
    conn = migrated_temp_db_connection
    importer = StatementImporter(conn)
    row = StructuredStatementRow(
        merchant_raw="PDF transaction",
        amount=Decimal("100.00"),
        currency="SGD",
        transaction_date=date(2024, 11, 1),
    )
    batch = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="test-no-fp-pdf",
    )
    assert len(batch.inserted_ids) == 1
    assert batch.idempotent_count == 0
