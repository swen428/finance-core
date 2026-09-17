"""Tests for Statement Import Runtime v1.

Covers:
  1. batch creation with deterministic public_id
  2. statement row insertion with dates preserved separately
  3. Decimal-safe money handling (exact round-trip)
  4. transaction_date vs posted_date preservation
  5. source row reference / evidence preservation
  6. deterministic public_id behavior (content-hash repeatable)
  7. duplicate import handling (safe rejection)
  8. empty rows rejected
  9. no database/finance.db access
 10. no mutation of unrelated tables
 11. dict input path
 12. StatementImportBatch metadata
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.financial_audit import FinancialAuditRepository, verify_financial_audit_chain
from finance_core.reconciliation.models import StatementAmountDirection
from finance_core.reconciliation.repository import (
    DuplicatePublicIdError,
    ReconciliationRepositoryError,
)
from finance_core.reconciliation.statement_import import (
    StatementImporter,
    StructuredStatementRow,
    derive_row_public_id,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"


def _sha(label: str) -> str:
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


def _read_amount(row: "sqlite3.Row") -> Decimal:
    """Safe Decimal read from SQLite NUMERIC column (avoids float loss)."""
    return Decimal(str(row["amount"]))


# ---------------------------------------------------------------------------
# 1. batch creation with deterministic public_id
# ---------------------------------------------------------------------------


def test_batch_creation_with_explicit_public_id(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    rows = [_make_row()]
    batch = importer.import_rows(
        rows,
        source_type="bank_statement",
        public_id="test-batch-explicit",
    )
    assert batch.batch_id > 0
    assert batch.public_id == "test-batch-explicit"
    assert batch.source_type == "bank_statement"
    assert batch.row_count == 1
    assert len(batch.inserted_ids) == 1


def test_batch_creation_with_auto_public_id(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    rows = [_make_row()]
    batch = importer.import_rows(rows, source_type="structured_csv")
    assert batch.public_id.startswith("statement-batch-")
    assert len(batch.public_id) == len("statement-batch-") + 64


# ---------------------------------------------------------------------------
# 2. statement row insertion with dates preserved separately
# ---------------------------------------------------------------------------


def test_dates_preserved_in_stored_row(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    txn_date = date(2024, 12, 1)
    posted_date = date(2024, 12, 3)
    row = _make_row(txn_date=txn_date, posted_date=posted_date)
    batch = importer.import_rows([row], source_type="bank_statement", public_id="batch-dates")

    stored = migrated_temp_db_connection.execute(
        "SELECT transaction_date, posted_date FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stored["transaction_date"] == "2024-12-01"
    assert stored["posted_date"] == "2024-12-03"


def test_null_dates_allowed(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row(txn_date=None, posted_date=None)
    batch = importer.import_rows([row], source_type="bank_statement", public_id="batch-null-dates")

    stored = migrated_temp_db_connection.execute(
        "SELECT transaction_date, posted_date FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stored["transaction_date"] is None
    assert stored["posted_date"] is None


# ---------------------------------------------------------------------------
# 3. Decimal-safe money handling
# ---------------------------------------------------------------------------


def test_decimal_amount_round_trips(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    amount = Decimal("29.90")
    row = _make_row(amount=amount)
    batch = importer.import_rows([row], source_type="bank_statement", public_id="batch-decimal")

    stored = migrated_temp_db_connection.execute(
        "SELECT amount FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    # Use str() to avoid SQLite NUMERIC affinity float loss
    assert _read_amount(stored) == amount


def test_zero_or_negative_amount_rejected() -> None:
    """Zero amount is allowed (for reversals); negative amount is still rejected."""
    _make_row(amount=Decimal("0.00"))  # zero now allowed
    with pytest.raises(ValueError, match="non-negative"):
        _make_row(amount=Decimal("-5.00"))


# ---------------------------------------------------------------------------
# 5. source row reference / evidence preservation
# ---------------------------------------------------------------------------


def test_source_row_reference_preserved(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row(statement_row_reference="csv-line-42")
    batch = importer.import_rows([row], source_type="bank_statement", public_id="batch-ref")

    stored = migrated_temp_db_connection.execute(
        "SELECT statement_row_reference FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stored["statement_row_reference"] == "csv-line-42"


def test_raw_row_payload_preserved(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    payload = {"csv_line": 3, "bank_id": "abc-123"}
    row = _make_row(raw_row_payload=payload)
    batch = importer.import_rows([row], source_type="bank_statement", public_id="batch-payload")

    stored = migrated_temp_db_connection.execute(
        "SELECT raw_row_payload_json FROM statement_transactions WHERE batch_id = ?",
        (batch.batch_id,),
    ).fetchone()
    assert stored["raw_row_payload_json"] is not None
    assert json.loads(stored["raw_row_payload_json"]) == payload


# ---------------------------------------------------------------------------
# 6. deterministic public_id behavior
# ---------------------------------------------------------------------------


def test_deterministic_row_public_id() -> None:
    """Same input always produces the same row public_id."""
    pid1 = derive_row_public_id("Apple", Decimal("10.00"), "SGD", date(2024, 12, 1))
    pid2 = derive_row_public_id("Apple", Decimal("10.00"), "SGD", date(2024, 12, 1))
    # With source identity included, stability is still guaranteed
    pid3 = derive_row_public_id(
        "Apple",
        Decimal("10.00"),
        "SGD",
        date(2024, 12, 1),
        source_file_hash="hash1",
        source_file_path="stmt.csv",
    )
    pid4 = derive_row_public_id(
        "Apple",
        Decimal("10.00"),
        "SGD",
        date(2024, 12, 1),
        source_file_hash="hash1",
        source_file_path="stmt.csv",
    )
    assert pid1 == pid2
    assert pid1.startswith("stmt-")
    assert pid3 == pid4
    assert pid1 != pid3  # different source identity changes the hash


def test_different_inputs_produce_different_public_ids() -> None:
    pid1 = derive_row_public_id("Apple", Decimal("10.00"), "SGD", date(2024, 12, 1))
    pid2 = derive_row_public_id("Netflix", Decimal("10.00"), "SGD", date(2024, 12, 1))
    assert pid1 != pid2

    # Same content, different source_file_hash → different IDs
    pid3 = derive_row_public_id(
        "Apple",
        Decimal("10.00"),
        "SGD",
        date(2024, 12, 1),
        source_file_hash="hashA",
    )
    pid4 = derive_row_public_id(
        "Apple",
        Decimal("10.00"),
        "SGD",
        date(2024, 12, 1),
        source_file_hash="hashB",
    )
    assert pid3 != pid4


# ---------------------------------------------------------------------------
# 7. duplicate import handling
# ---------------------------------------------------------------------------


def test_duplicate_import_safely_skipped(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row()

    # First import with source identity
    batch1 = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="batch-dup-1",
        source_file_hash=_sha("abc123"),
    )
    assert batch1.skipped_duplicates == 0
    assert len(batch1.inserted_ids) == 1

    # Re-import same row with same source identity (different batch pubid
    # but same source_file_hash/path) -- public_id is source-row-derived,
    # so it collides deterministically
    batch2 = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="batch-dup-2",
        source_file_hash=_sha("abc123"),
    )
    assert batch2.skipped_duplicates == 0
    assert batch2.idempotent_count == 1
    assert batch2.batch_id == batch1.batch_id
    assert batch2.public_id == batch1.public_id
    assert len(batch2.inserted_ids) == 0

    # Re-import same row from a *different* source -- should NOT collide
    batch3 = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="batch-dup-3",
        source_file_hash=_sha("def456"),
    )
    assert batch3.skipped_duplicates == 0
    assert len(batch3.inserted_ids) == 1
    assert batch2.skipped_duplicates == 0
    assert len(batch2.inserted_ids) == 0


def test_partial_duplicate_batch(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    row1 = _make_row(merchant="Apple", amount=Decimal("10.00"))
    row2 = _make_row(merchant="Netflix", amount=Decimal("15.99"))

    # Import both with source identity
    batch1 = importer.import_rows(
        [row1, row2],
        source_type="bank_statement",
        public_id="batch-partial-1",
        source_file_hash=_sha("hash123"),
    )
    assert batch1.row_count == 2
    assert batch1.skipped_duplicates == 0
    assert len(batch1.inserted_ids) == 2

    # Re-import row1 (different batch, *different* source identity) + row3
    row3 = _make_row(merchant="Spotify", amount=Decimal("9.99"), txn_date=date(2024, 12, 15))
    batch2 = importer.import_rows(
        [row1, row3],
        source_type="bank_statement",
        public_id="batch-partial-2",
        source_file_hash=_sha("hash456"),
    )
    assert batch2.row_count == 2
    assert batch2.skipped_duplicates == 0  # Different source → not a duplicate
    assert len(batch2.inserted_ids) == 2

    # A subset of a source already owned by batch1 is a changed command, not
    # a successful partial-duplicate batch.
    with pytest.raises(DuplicatePublicIdError, match="source content already belongs"):
        importer.import_rows(
            [row1],
            source_type="bank_statement",
            public_id="batch-partial-3",
            source_file_hash=_sha("hash123"),
        )

    # Verify DB has 4 unique rows
    count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) FROM statement_transactions"
    ).fetchone()[0]
    assert count == 4


def test_same_batch_public_id_reimport_is_idempotent(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row(statement_row_reference="stmt-line-1")

    first = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="batch-repeatable",
        source_file_hash=_sha("repeat-hash"),
    )
    second = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="batch-repeatable",
        source_file_hash=_sha("repeat-hash"),
    )

    assert second.batch_id == first.batch_id
    assert second.public_id == first.public_id
    assert second.skipped_duplicates == 0
    assert second.idempotent_count == 1
    assert second.inserted_ids == []

    batch_count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) FROM statement_import_batches WHERE public_id = ?",
        ("batch-repeatable",),
    ).fetchone()[0]
    row_count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) FROM statement_transactions WHERE batch_id = ?",
        (first.batch_id,),
    ).fetchone()[0]
    assert batch_count == 1
    assert row_count == 1


def test_same_public_id_with_different_payload_is_conflict(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row(
        statement_row_reference="stmt-line-1",
        raw_row_payload={"version": 1},
    )
    changed_row = _make_row(
        statement_row_reference="stmt-line-1",
        raw_row_payload={"version": 2},
    )

    importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="batch-row-conflict",
        source_file_hash=_sha("same-source"),
    )

    with pytest.raises(DuplicatePublicIdError, match="source content already belongs"):
        importer.import_rows(
            [changed_row],
            source_type="bank_statement",
            public_id="batch-row-conflict",
            source_file_hash=_sha("same-source"),
        )

    stored_payloads = migrated_temp_db_connection.execute(
        """
        SELECT raw_row_payload_json FROM statement_transactions
        WHERE batch_id = (
          SELECT id FROM statement_import_batches
          WHERE public_id = 'batch-row-conflict'
        )
        ORDER BY id
        """
    ).fetchall()
    assert [json.loads(row["raw_row_payload_json"]) for row in stored_payloads] == [{"version": 1}]


def test_same_row_fingerprint_with_changed_direction_is_conflict(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row(
        statement_row_reference="stmt-line-1",
        row_fingerprint=_sha("csv-same-fingerprint"),
        amount_direction=StatementAmountDirection.DEBIT,
        raw_amount="10.00",
    )
    changed_row = _make_row(
        statement_row_reference="stmt-line-1",
        row_fingerprint=_sha("csv-same-fingerprint"),
        amount_direction=StatementAmountDirection.REFUND,
        raw_amount="-10.00",
    )

    importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="batch-fingerprint-conflict",
        source_file_hash=_sha("same-source"),
    )

    with pytest.raises(DuplicatePublicIdError, match="source content already belongs"):
        importer.import_rows(
            [changed_row],
            source_type="bank_statement",
            public_id="batch-fingerprint-conflict",
            source_file_hash=_sha("same-source"),
        )

    stored = migrated_temp_db_connection.execute(
        """
        SELECT amount_direction, raw_amount FROM statement_transactions
        WHERE batch_id = (
          SELECT id FROM statement_import_batches
          WHERE public_id = 'batch-fingerprint-conflict'
        )
        """
    ).fetchone()
    assert stored["amount_direction"] == "debit"
    assert stored["raw_amount"] == "10.00"


def test_same_batch_public_id_with_different_source_metadata_rejected(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row(statement_row_reference="stmt-line-1")

    importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="batch-conflicting-source",
        source_file_hash=_sha("hash-a"),
    )

    with pytest.raises(DuplicatePublicIdError, match="batch-conflicting-source"):
        importer.import_rows(
            [row],
            source_type="bank_statement",
            public_id="batch-conflicting-source",
            source_file_hash=_sha("hash-b"),
        )

    row_count = migrated_temp_db_connection.execute(
        """
        SELECT COUNT(*) FROM statement_transactions
        WHERE batch_id = (
          SELECT id FROM statement_import_batches
          WHERE public_id = 'batch-conflicting-source'
        )
        """
    ).fetchone()[0]
    assert row_count == 1


# ---------------------------------------------------------------------------
# 8. empty rows rejected
# ---------------------------------------------------------------------------


def test_empty_rows_rejected(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    with pytest.raises(ValueError, match="must not be empty"):
        importer.import_rows([], source_type="bank_statement")


# ---------------------------------------------------------------------------
# 9. no database/finance.db access
# ---------------------------------------------------------------------------


def test_importer_does_not_touch_live_db(
    migrated_temp_db_connection: "sqlite3.Connection",
    temp_db_path: Path,
) -> None:
    database_path = migrated_temp_db_connection.execute("PRAGMA database_list").fetchone()["file"]
    assert Path(database_path) == temp_db_path
    assert Path(database_path) != LIVE_DB_PATH


# ---------------------------------------------------------------------------
# 10. no mutation of unrelated tables
# ---------------------------------------------------------------------------


def test_no_unrelated_table_mutation(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row()

    before_tables = _table_counts(migrated_temp_db_connection)

    importer.import_rows([row], source_type="bank_statement", public_id="batch-isolated")

    after_tables = _table_counts(migrated_temp_db_connection)

    for table_name in before_tables:
        if table_name in (
            "statement_import_batches",
            "statement_transactions",
            "financial_audit_events",
        ):
            assert after_tables[table_name] > before_tables[table_name], (
                f"Expected {table_name} to grow"
            )
        else:
            assert after_tables[table_name] == before_tables[table_name], (
                f"Unexpected mutation in {table_name}"
            )


def test_statement_import_acceptance_is_audited_once_on_replay(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row(statement_row_reference="audit-row-1")
    first = importer.import_rows([row], source_type="bank_statement", public_id="batch-audited")
    second = importer.import_rows([row], source_type="bank_statement", public_id="batch-audited")
    assert first.inserted_ids
    assert second.idempotent_count + second.skipped_duplicates == 1
    events = FinancialAuditRepository(migrated_temp_db_connection).list_chain(
        "statement_import_batch", "batch-audited"
    )
    assert len(events) == 1
    assert events[0].event_type == "statement_import_accepted"
    assert events[0].actor_public_id == "statement-importer"
    verification = verify_financial_audit_chain(
        migrated_temp_db_connection,
        aggregate_type="statement_import_batch",
        aggregate_public_id="batch-audited",
    )
    assert verification.valid is True


def test_batch_with_deleted_audit_history_fails_closed_on_replay(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row(statement_row_reference="legacy-row-1")
    importer.import_rows([row], source_type="bank_statement", public_id="batch-legacy")
    migrated_temp_db_connection.execute("DROP TRIGGER trg_financial_audit_events_no_delete")
    migrated_temp_db_connection.execute(
        "DELETE FROM financial_audit_events WHERE aggregate_public_id = ?",
        ("batch-legacy",),
    )
    migrated_temp_db_connection.commit()

    with pytest.raises(ReconciliationRepositoryError, match="audit chain is missing"):
        importer.import_rows([row], source_type="bank_statement", public_id="batch-legacy")

    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM financial_audit_events WHERE aggregate_public_id = ?",
            ("batch-legacy",),
        ).fetchone()[0]
        == 0
    )


def test_audit_insert_failure_rolls_back_statement_import(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    migrated_temp_db_connection.execute(
        """CREATE TRIGGER test_fail_statement_audit
        BEFORE INSERT ON financial_audit_events
        WHEN NEW.aggregate_type = 'statement_import_batch'
        BEGIN SELECT RAISE(ABORT, 'injected statement audit failure'); END"""
    )
    migrated_temp_db_connection.commit()
    importer = StatementImporter(migrated_temp_db_connection)
    with pytest.raises(sqlite3.IntegrityError, match="injected statement audit failure"):
        importer.import_rows(
            [_make_row()],
            source_type="bank_statement",
            public_id="batch-audit-failure",
        )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM statement_import_batches WHERE public_id = ?",
            ("batch-audit-failure",),
        ).fetchone()[0]
        == 0
    )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM statement_transactions"
        ).fetchone()[0]
        == 0
    )
    assert (
        migrated_temp_db_connection.execute(
            "SELECT COUNT(*) FROM financial_audit_events"
        ).fetchone()[0]
        == 0
    )


def _table_counts(conn: "sqlite3.Connection") -> dict[str, int]:
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    counts: dict[str, int] = {}
    for (name,) in tables:
        n = conn.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]
        counts[name] = n
    return counts


# ---------------------------------------------------------------------------
# 11. dict input path
# ---------------------------------------------------------------------------


def test_dict_input_path(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    rows: list[dict] = [
        {
            "merchant_raw": "Apple",
            "amount": Decimal("10.00"),
            "currency": "SGD",
            "transaction_date": date(2024, 12, 1),
            "posted_date": date(2024, 12, 3),
        },
        {
            "merchant_raw": "Netflix",
            "amount": "15.99",
            "currency": "SGD",
            "transaction_date": "2024-12-02",
        },
    ]
    batch = importer.import_rows(rows, source_type="structured_csv", public_id="batch-dict")
    assert batch.row_count == 2
    assert len(batch.inserted_ids) == 2

    stored = migrated_temp_db_connection.execute(
        "SELECT merchant_raw, amount FROM statement_transactions WHERE batch_id = ? ORDER BY id",
        (batch.batch_id,),
    ).fetchall()
    assert stored[0]["merchant_raw"] == "Apple"
    assert _read_amount(stored[0]) == Decimal("10.00")
    assert stored[1]["merchant_raw"] == "Netflix"
    assert _read_amount(stored[1]) == Decimal("15.99")


# ---------------------------------------------------------------------------
# 12. StatementImportBatch metadata
# ---------------------------------------------------------------------------


def test_statement_import_batch_metadata(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    importer = StatementImporter(migrated_temp_db_connection)
    rows = [
        _make_row(merchant="Apple", amount=Decimal("10.00")),
        _make_row(merchant="Netflix", amount=Decimal("15.99")),
    ]
    batch = importer.import_rows(
        rows,
        source_type="credit_card_statement",
        public_id="batch-meta",
        account_name="Owner Visa",
        currency="SGD",
        statement_period_start="2024-12-01",
        statement_period_end="2024-12-31",
    )

    assert batch.batch_id > 0
    assert batch.public_id == "batch-meta"
    assert batch.source_type == "credit_card_statement"
    assert batch.row_count == 2
    assert batch.skipped_duplicates == 0
    assert len(batch.inserted_ids) == 2

    stored = migrated_temp_db_connection.execute(
        "SELECT * FROM statement_import_batches WHERE id = ?", (batch.batch_id,)
    ).fetchone()
    assert stored["account_name"] == "Owner Visa"
    assert stored["currency"] == "SGD"
    assert stored["statement_period_start"] == "2024-12-01"
    assert stored["statement_period_end"] == "2024-12-31"


# ---------------------------------------------------------------------------
# 13. Distinct same-content rows: different statement_row_reference → both inserted
# ---------------------------------------------------------------------------


def test_distinct_rows_with_different_ref_both_inserted(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Two rows with same merchant/date/amount/currency but different
    statement_row_reference produce different public_ids and are both inserted."""
    importer = StatementImporter(migrated_temp_db_connection)
    row1 = _make_row(
        merchant="GrabFood",
        amount=Decimal("12.50"),
        currency="SGD",
        txn_date=date(2024, 12, 1),
        statement_row_reference="grab-ref-001",
    )
    row2 = _make_row(
        merchant="GrabFood",
        amount=Decimal("12.50"),
        currency="SGD",
        txn_date=date(2024, 12, 1),
        statement_row_reference="grab-ref-002",
    )
    batch = importer.import_rows(
        [row1, row2], source_type="bank_statement", public_id="batch-distinct-ref"
    )
    assert batch.row_count == 2
    assert batch.skipped_duplicates == 0
    assert len(batch.inserted_ids) == 2

    # Verify both are in the DB
    rows = migrated_temp_db_connection.execute(
        "SELECT public_id, statement_row_reference FROM statement_transactions"
        " WHERE batch_id = ? ORDER BY id",
        (batch.batch_id,),
    ).fetchall()
    assert len(rows) == 2
    refs = {r["statement_row_reference"] for r in rows}
    assert refs == {"grab-ref-001", "grab-ref-002"}
    # Public IDs must be different
    assert rows[0]["public_id"] != rows[1]["public_id"]


# ---------------------------------------------------------------------------
# 14. Distinct same-content rows: different source_row_index → both inserted
# ---------------------------------------------------------------------------


def test_distinct_rows_with_different_row_index_both_inserted(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Two rows with same merchant/date/amount/currency and no
    statement_row_reference produce different public_ids via source_row_index
    and are both inserted."""
    importer = StatementImporter(migrated_temp_db_connection)
    row1 = _make_row(
        merchant="GrabFood",
        amount=Decimal("12.50"),
        currency="SGD",
        txn_date=date(2024, 12, 1),
    )
    row2 = _make_row(
        merchant="GrabFood",
        amount=Decimal("12.50"),
        currency="SGD",
        txn_date=date(2024, 12, 1),
    )
    batch = importer.import_rows(
        [row1, row2], source_type="bank_statement", public_id="batch-distinct-idx"
    )
    assert batch.row_count == 2
    assert batch.skipped_duplicates == 0
    assert len(batch.inserted_ids) == 2

    rows = migrated_temp_db_connection.execute(
        "SELECT public_id FROM statement_transactions WHERE batch_id = ? ORDER BY id",
        (batch.batch_id,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["public_id"] != rows[1]["public_id"]


# ---------------------------------------------------------------------------
# 15. Re-import exact same rows is deterministic skip
# ---------------------------------------------------------------------------


def test_reimport_exact_rows_deterministic_skip(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Re-importing the exact same rows (same content, same batch) skips
    duplicates deterministically — same public_ids produced."""
    importer = StatementImporter(migrated_temp_db_connection)
    row1 = _make_row(
        merchant="Spotify",
        amount=Decimal("9.99"),
        currency="SGD",
        txn_date=date(2024, 12, 1),
        statement_row_reference="spotify-001",
    )
    row2 = _make_row(
        merchant="Netflix",
        amount=Decimal("15.99"),
        currency="SGD",
        txn_date=date(2024, 12, 2),
        statement_row_reference="netflix-002",
    )

    # First import with source identity
    batch1 = importer.import_rows(
        [row1, row2],
        source_type="bank_statement",
        public_id="batch-reimport-1",
        source_file_hash=_sha("hash123"),
    )
    assert batch1.skipped_duplicates == 0
    assert len(batch1.inserted_ids) == 2

    # Re-import identical rows with *same* source identity → duplicates
    batch2 = importer.import_rows(
        [row1, row2],
        source_type="bank_statement",
        public_id="batch-reimport-2",
        source_file_hash=_sha("hash123"),
    )
    assert batch2.skipped_duplicates == 0
    assert batch2.idempotent_count == 2
    assert batch2.batch_id == batch1.batch_id
    assert len(batch2.inserted_ids) == 0

    # Re-import same rows with *different* source → NOT duplicates
    batch3 = importer.import_rows(
        [row1, row2],
        source_type="bank_statement",
        public_id="batch-reimport-3",
        source_file_hash=_sha("hash456"),
    )
    assert batch3.skipped_duplicates == 0
    assert len(batch3.inserted_ids) == 2

    # DB has 4 rows: 2 original + 2 from different source
    count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) FROM statement_transactions"
    ).fetchone()[0]
    assert count == 4


# ---------------------------------------------------------------------------
# 16. derive_row_public_id stability with full input
# ---------------------------------------------------------------------------


def test_derive_row_public_id_stable_with_full_input() -> None:
    """derive_row_public_id is stable across calls with the same full input."""
    kwargs = dict(
        merchant_raw="Apple",
        amount=Decimal("10.00"),
        currency="SGD",
        transaction_date=date(2024, 12, 1),
        posted_date=date(2024, 12, 3),
        statement_row_reference="ref-42",
        source_row_index=7,
        source_file_hash="hash123",
        source_file_path="bank-a.csv",
        account_name="Owner Visa",
    )
    pid1 = derive_row_public_id(**kwargs)
    pid2 = derive_row_public_id(**kwargs)
    assert pid1 == pid2
    assert pid1.startswith("stmt-")


def test_derive_row_public_id_different_posted_date_different_id() -> None:
    """Different posted_date produces different public_id."""
    pid1 = derive_row_public_id(
        "Apple",
        Decimal("10.00"),
        "SGD",
        date(2024, 12, 1),
        posted_date=date(2024, 12, 3),
    )
    pid2 = derive_row_public_id(
        "Apple",
        Decimal("10.00"),
        "SGD",
        date(2024, 12, 1),
        posted_date=date(2024, 12, 4),
    )
    assert pid1 != pid2


def test_derive_row_public_id_different_ref_different_id() -> None:
    """Different statement_row_reference produces different public_id."""
    pid1 = derive_row_public_id(
        "Apple",
        Decimal("10.00"),
        "SGD",
        date(2024, 12, 1),
        statement_row_reference="ref-1",
    )
    pid2 = derive_row_public_id(
        "Apple",
        Decimal("10.00"),
        "SGD",
        date(2024, 12, 1),
        statement_row_reference="ref-2",
    )
    assert pid1 != pid2


# ---------------------------------------------------------------------------
# 17. Cross-source identity: same row ref + different source_file_hash → both inserted
# ---------------------------------------------------------------------------


def test_same_row_different_source_hash_both_inserted(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Two rows with identical visible transaction fields and statement_row_reference
    but different source_file_hash produce different public_ids and are both inserted."""
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row(
        merchant="GrabFood",
        amount=Decimal("12.50"),
        currency="SGD",
        txn_date=date(2024, 12, 1),
        statement_row_reference="csv-line-2",
    )
    # Import from source A
    batch1 = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="batch-src-a",
        source_file_hash=_sha("hashAAA"),
    )
    assert batch1.skipped_duplicates == 0
    assert len(batch1.inserted_ids) == 1

    # Import same row from source B (different source_hash)
    batch2 = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="batch-src-b",
        source_file_hash=_sha("hashBBB"),
    )
    assert batch2.skipped_duplicates == 0
    assert len(batch2.inserted_ids) == 1

    # Both are in DB with different public_ids
    rows = migrated_temp_db_connection.execute(
        "SELECT public_id FROM statement_transactions ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["public_id"] != rows[1]["public_id"]


# ---------------------------------------------------------------------------
# 18. Cross-source identity: same row ref + different account → both inserted
# ---------------------------------------------------------------------------


def test_same_row_different_account_both_inserted(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Two rows with identical visible transaction fields but different account_name
    produce different public_ids and are both inserted."""
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row(
        merchant="GrabFood",
        amount=Decimal("12.50"),
        currency="SGD",
        txn_date=date(2024, 12, 1),
        statement_row_reference="csv-line-2",
    )
    # Import from Owner Visa
    batch1 = importer.import_rows(
        [row],
        source_type="credit_card_statement",
        public_id="batch-owner-visa",
        account_name="Owner Visa",
        source_file_hash=_sha("hashVisa"),
    )
    assert batch1.skipped_duplicates == 0

    # Import from Owner Mastercard
    batch2 = importer.import_rows(
        [row],
        source_type="credit_card_statement",
        public_id="batch-owner-mc",
        account_name="Owner Mastercard",
        source_file_hash=_sha("hashMC"),
    )
    assert batch2.skipped_duplicates == 0
    assert len(batch2.inserted_ids) == 1

    rows = migrated_temp_db_connection.execute(
        """
        SELECT st.public_id, bat.account_name
        FROM statement_transactions st
        JOIN statement_import_batches bat ON st.batch_id = bat.id
        ORDER BY st.id
        """
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["public_id"] != rows[1]["public_id"]


# ---------------------------------------------------------------------------
# 19. Same source identity + same row ref → duplicate skipped
# ---------------------------------------------------------------------------


def test_same_source_same_row_duplicate_skipped(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Same source identity (source_file_hash) + same row reference re-imported
    is deterministically skipped as duplicate."""
    importer = StatementImporter(migrated_temp_db_connection)
    row = _make_row(
        merchant="GrabFood",
        amount=Decimal("12.50"),
        currency="SGD",
        txn_date=date(2024, 12, 1),
        statement_row_reference="csv-line-2",
    )
    # First import
    batch1 = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="batch-same-1",
        source_file_hash=_sha("sameHash"),
    )
    assert batch1.skipped_duplicates == 0

    # Re-import with same source_file_hash (different batch public_id still ok)
    batch2 = importer.import_rows(
        [row],
        source_type="bank_statement",
        public_id="batch-same-2",
        source_file_hash=_sha("sameHash"),
    )
    assert batch2.skipped_duplicates == 0
    assert batch2.idempotent_count == 1
    assert batch2.batch_id == batch1.batch_id
    assert len(batch2.inserted_ids) == 0


# ---------------------------------------------------------------------------
# 20. Cross-source: same csv-line-N from different source identities → both inserted
# ---------------------------------------------------------------------------


def test_same_csv_line_different_source_identity_both_inserted(
    migrated_temp_db_connection: "sqlite3.Connection",
) -> None:
    """Two rows with auto-assigned csv-line-N and same visible fields from
    two different CSV sources (different source_file_hash) produce different
    public_ids and are NOT collapsed."""
    importer = StatementImporter(migrated_temp_db_connection)
    row1 = _make_row(
        merchant="GrabFood",
        amount=Decimal("12.50"),
        currency="SGD",
        txn_date=date(2024, 12, 1),
        statement_row_reference="csv-line-2",
    )
    row2 = _make_row(
        merchant="GrabFood",
        amount=Decimal("12.50"),
        currency="SGD",
        txn_date=date(2024, 12, 1),
        statement_row_reference="csv-line-2",
    )

    # Import from source A
    batch_a = importer.import_rows(
        [row1],
        source_type="bank_statement",
        public_id="batch-csv-a",
        source_file_hash=_sha("hashA"),
    )
    assert batch_a.skipped_duplicates == 0
    assert len(batch_a.inserted_ids) == 1

    # Import same csv-line-2 from source B — must NOT be collapsed
    batch_b = importer.import_rows(
        [row2],
        source_type="bank_statement",
        public_id="batch-csv-b",
        source_file_hash=_sha("hashB"),
    )
    assert batch_b.skipped_duplicates == 0
    assert len(batch_b.inserted_ids) == 1

    # Both rows exist in DB
    count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) FROM statement_transactions"
    ).fetchone()[0]
    assert count == 2


# ---------------------------------------------------------------------------
# 21. derive_row_public_id different source_file_hash different id
# ---------------------------------------------------------------------------


def test_derive_row_public_id_different_source_hash_different_id() -> None:
    """Different source_file_hash with same row content produces different public_id."""
    kwargs = dict(
        merchant_raw="Apple",
        amount=Decimal("10.00"),
        currency="SGD",
        transaction_date=date(2024, 12, 1),
        posted_date=date(2024, 12, 3),
        statement_row_reference="ref-42",
        source_row_index=7,
    )
    pid1 = derive_row_public_id(**kwargs, source_file_hash="hashA")
    pid2 = derive_row_public_id(**kwargs, source_file_hash="hashB")
    assert pid1 != pid2


def test_derive_row_public_id_different_account_different_id() -> None:
    """Different account_name with same row content produces different public_id."""
    kwargs = dict(
        merchant_raw="Apple",
        amount=Decimal("10.00"),
        currency="SGD",
        transaction_date=date(2024, 12, 1),
    )
    pid1 = derive_row_public_id(**kwargs, account_name="Owner Visa")
    pid2 = derive_row_public_id(**kwargs, account_name="Owner Mastercard")
    assert pid1 != pid2
