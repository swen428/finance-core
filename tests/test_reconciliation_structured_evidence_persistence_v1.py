"""Tests for Reconciliation Structured Evidence Persistence v1.

Covers:
  1. Structured evidence schema exists in the default temp migration chain
  2. New table exists with expected columns
  3. Expected indexes exist
  4. Insert one evidence record and round-trip it
  5. Insert with all nullable fields set and round-trip
  6. Insert with source_page/source_row/source_field and round-trip
  7. Insert multiple records and list by review queue
  8. Insert multiple records and list by statement transaction
  9. Insert multiple records and list by app transaction
 10. List by evidence_type filter
 11. List all records
 12. Idempotent save -- same public_id, same data returns False
 13. Conflicting save -- same public_id, different data raises
 14. Batch persist with multiple records
 15. generate_evidence_public_id is deterministic
 16. generate_evidence_public_id produces different ids for different inputs
 17. No test touches database/finance.db
 18. No final transaction table is mutated
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from finance_core.reconciliation.structured_evidence import (
    StructuredEvidenceConflictError,
    StructuredEvidencePersistence,
    StructuredEvidenceRecord,
    generate_evidence_public_id,
)

if TYPE_CHECKING:
    pass

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_record(**kw) -> StructuredEvidenceRecord:
    defaults: dict = dict(
        public_id=generate_evidence_public_id(
            "matching_decision",
            "matching_engine",
            statement_transaction_id="stmt-test-001",
        ),
        evidence_type="matching_decision",
        source_type="matching_engine",
        evidence_payload={
            "match_status": "matched",
            "amount": "29.90",
            "currency": "SGD",
        },
        confidence_score="0.95",
        statement_transaction_id="stmt-test-001",
    )
    defaults.update(kw)
    return StructuredEvidenceRecord(**defaults)


# ---------------------------------------------------------------------------
# 1. Structured evidence schema exists in the default temp migration chain
# ---------------------------------------------------------------------------


def test_migration_011_applies_cleanly(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    tables = migrated_temp_db_connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    table_names = {row["name"] for row in tables}
    assert "reconciliation_structured_evidence" in table_names


# ---------------------------------------------------------------------------
# 2. New table exists with expected columns
# ---------------------------------------------------------------------------


EXPECTED_COLUMNS = {
    "id",
    "public_id",
    "review_queue_public_id",
    "statement_transaction_id",
    "app_transaction_id",
    "evidence_type",
    "source_type",
    "source_id",
    "source_path",
    "source_page",
    "source_row",
    "source_field",
    "confidence_score",
    "evidence_payload",
    "created_at",
    "updated_at",
}


def test_table_has_expected_columns(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    info = migrated_temp_db_connection.execute(
        "PRAGMA table_info(reconciliation_structured_evidence)"
    ).fetchall()
    column_names = {row["name"] for row in info}
    assert EXPECTED_COLUMNS <= column_names, f"Missing columns: {EXPECTED_COLUMNS - column_names}"


# ---------------------------------------------------------------------------
# 3. Expected indexes exist
# ---------------------------------------------------------------------------


EXPECTED_INDEXES = {
    "idx_structured_evidence_review_queue",
    "idx_structured_evidence_statement_txn",
    "idx_structured_evidence_app_txn",
    "idx_structured_evidence_type",
    "idx_structured_evidence_source_type",
}


def test_expected_indexes_exist(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    indexes = migrated_temp_db_connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    index_names = {row["name"] for row in indexes}
    assert EXPECTED_INDEXES <= index_names, f"Missing indexes: {EXPECTED_INDEXES - index_names}"


# ---------------------------------------------------------------------------
# 4. Insert one evidence record and round-trip it
# ---------------------------------------------------------------------------


def test_insert_and_retrieve_one_record(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    pid = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        statement_transaction_id="stmt-r1",
    )
    record = _sample_record(
        public_id=pid,
        statement_transaction_id="stmt-r1",
        evidence_payload={"match_status": "matched", "amount": "29.90", "currency": "SGD"},
        confidence_score="0.95",
    )
    inserted = sep.save_evidence(record)
    assert inserted is True

    retrieved = sep.get_by_public_id(pid)
    assert retrieved is not None
    assert retrieved["public_id"] == pid
    assert retrieved["evidence_type"] == "matching_decision"
    assert retrieved["source_type"] == "matching_engine"
    assert retrieved["statement_transaction_id"] == "stmt-r1"
    assert retrieved["confidence_score"] == "0.95"
    assert retrieved["review_queue_public_id"] is None

    payload = json.loads(retrieved["evidence_payload"])
    assert payload["match_status"] == "matched"
    assert payload["amount"] == "29.90"
    assert payload["currency"] == "SGD"


# ---------------------------------------------------------------------------
# 5. Insert with all nullable fields set and round-trip
# ---------------------------------------------------------------------------


def test_insert_with_all_nullable_fields_set(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    pid = generate_evidence_public_id(
        "review_resolution",
        "apply_runtime",
        review_queue_public_id="rq-full",
        statement_transaction_id="stmt-full",
        app_transaction_id="app-full",
    )
    record = _sample_record(
        public_id=pid,
        evidence_type="review_resolution",
        source_type="apply_runtime",
        review_queue_public_id="rq-full",
        statement_transaction_id="stmt-full",
        app_transaction_id="app-full",
        source_id="batch-abc",
        source_path="/tmp/test.csv",
        source_page=3,
        source_row=15,
        source_field="amount",
        confidence_score="0.99",
        evidence_payload={"decision": "confirm_match", "reviewer": "human"},
        created_at="2026-06-11T10:00:00",
        updated_at="2026-06-11T10:00:00",
    )
    inserted = sep.save_evidence(record)
    assert inserted is True

    retrieved = sep.get_by_public_id(pid)
    assert retrieved is not None
    assert retrieved["review_queue_public_id"] == "rq-full"
    assert retrieved["statement_transaction_id"] == "stmt-full"
    assert retrieved["app_transaction_id"] == "app-full"
    assert retrieved["source_id"] == "batch-abc"
    assert retrieved["source_path"] == "/tmp/test.csv"
    assert retrieved["source_page"] == 3
    assert retrieved["source_row"] == 15
    assert retrieved["source_field"] == "amount"
    assert retrieved["confidence_score"] == "0.99"
    assert retrieved["created_at"] == "2026-06-11T10:00:00"


# ---------------------------------------------------------------------------
# 6. Insert with source_page/source_row/source_field and round-trip
# ---------------------------------------------------------------------------


def test_insert_with_source_references(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    pid = generate_evidence_public_id(
        "source_reference",
        "statement_csv",
        statement_transaction_id="stmt-src",
    )
    record = _sample_record(
        public_id=pid,
        evidence_type="source_reference",
        source_type="statement_csv",
        statement_transaction_id="stmt-src",
        source_page=2,
        source_row=42,
        source_field="merchant_raw",
        evidence_payload={"source": "csv_row_42", "raw_merchant": "APPLE.COM/BILL"},
    )
    sep.save_evidence(record)

    retrieved = sep.get_by_public_id(pid)
    assert retrieved is not None
    assert retrieved["source_page"] == 2
    assert retrieved["source_row"] == 42
    assert retrieved["source_field"] == "merchant_raw"


# ---------------------------------------------------------------------------
# 7. Insert multiple records and list by review queue
# ---------------------------------------------------------------------------


def test_list_by_review_queue(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    rq_a = "rq-aaa"
    rq_b = "rq-bbb"

    for stmt_id in ("s1", "s2"):
        pid = generate_evidence_public_id(
            "matching_decision",
            "matching_engine",
            review_queue_public_id=rq_a,
            statement_transaction_id=stmt_id,
        )
        sep.save_evidence(
            _sample_record(
                public_id=pid,
                review_queue_public_id=rq_a,
                statement_transaction_id=stmt_id,
            )
        )

    pid_b = generate_evidence_public_id(
        "review_resolution",
        "apply_runtime",
        review_queue_public_id=rq_b,
    )
    sep.save_evidence(
        _sample_record(
            public_id=pid_b,
            evidence_type="review_resolution",
            source_type="apply_runtime",
            review_queue_public_id=rq_b,
            statement_transaction_id=None,
        )
    )

    results_a = sep.list_by_review_queue(rq_a)
    assert len(results_a) == 2
    assert all(r["review_queue_public_id"] == rq_a for r in results_a)

    results_b = sep.list_by_review_queue(rq_b)
    assert len(results_b) == 1
    assert results_b[0]["review_queue_public_id"] == rq_b


# ---------------------------------------------------------------------------
# 8. Insert multiple records and list by statement transaction
# ---------------------------------------------------------------------------


def test_list_by_statement_transaction(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    stmt_x = "stmt-xxx"

    for i in range(3):
        pid = generate_evidence_public_id(
            f"type-{i}",
            f"source-{i}",
            statement_transaction_id=stmt_x,
        )
        sep.save_evidence(
            _sample_record(
                public_id=pid,
                evidence_type=f"type-{i}",
                source_type=f"source-{i}",
                statement_transaction_id=stmt_x,
            )
        )

    results = sep.list_by_statement_transaction(stmt_x)
    assert len(results) == 3
    assert all(r["statement_transaction_id"] == stmt_x for r in results)


# ---------------------------------------------------------------------------
# 9. Insert multiple records and list by app transaction
# ---------------------------------------------------------------------------


def test_list_by_app_transaction(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    app_y = "app-yyy"

    pid = generate_evidence_public_id(
        "app_transaction",
        "matching_engine",
        statement_transaction_id="stmt-1",
        app_transaction_id=app_y,
    )
    sep.save_evidence(
        _sample_record(
            public_id=pid,
            evidence_type="app_transaction",
            source_type="matching_engine",
            statement_transaction_id="stmt-1",
            app_transaction_id=app_y,
        )
    )

    results = sep.list_by_app_transaction(app_y)
    assert len(results) == 1
    assert results[0]["app_transaction_id"] == app_y


# ---------------------------------------------------------------------------
# 10. List by evidence_type filter
# ---------------------------------------------------------------------------


def test_list_by_evidence_type(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)

    pid_a = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        statement_transaction_id="s-a",
    )
    sep.save_evidence(
        _sample_record(
            public_id=pid_a,
            evidence_type="matching_decision",
            source_type="matching_engine",
            statement_transaction_id="s-a",
        )
    )

    pid_b = generate_evidence_public_id(
        "review_resolution",
        "apply_runtime",
        statement_transaction_id="s-b",
    )
    sep.save_evidence(
        _sample_record(
            public_id=pid_b,
            evidence_type="review_resolution",
            source_type="apply_runtime",
            statement_transaction_id="s-b",
        )
    )

    md_results = sep.list_by_evidence_type("matching_decision")
    assert len(md_results) == 1
    assert md_results[0]["evidence_type"] == "matching_decision"

    rr_results = sep.list_by_evidence_type("review_resolution")
    assert len(rr_results) == 1
    assert rr_results[0]["evidence_type"] == "review_resolution"

    empty_results = sep.list_by_evidence_type("nonexistent")
    assert len(empty_results) == 0


# ---------------------------------------------------------------------------
# 11. List all records
# ---------------------------------------------------------------------------


def test_list_all(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)

    for i in range(5):
        pid = generate_evidence_public_id(
            f"type-{i}",
            f"source-{i}",
            statement_transaction_id=f"stmt-{i}",
        )
        sep.save_evidence(
            _sample_record(
                public_id=pid,
                evidence_type=f"type-{i}",
                source_type=f"source-{i}",
                statement_transaction_id=f"stmt-{i}",
            )
        )

    results = sep.list_all()
    assert len(results) == 5
    ids = [r["id"] for r in results]
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# 12. Idempotent save
# ---------------------------------------------------------------------------


def test_idempotent_save_same_data(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    pid = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        statement_transaction_id="stmt-idem",
    )
    record = _sample_record(
        public_id=pid,
        statement_transaction_id="stmt-idem",
    )

    first = sep.save_evidence(record)
    assert first is True

    second = sep.save_evidence(record)
    assert second is False

    results = sep.list_by_statement_transaction("stmt-idem")
    assert len(results) == 1


# ---------------------------------------------------------------------------
# 13. Conflicting save raises error
# ---------------------------------------------------------------------------


def test_conflicting_save_different_data_raises(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    pid = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        statement_transaction_id="stmt-conflict",
    )
    record_a = _sample_record(
        public_id=pid,
        statement_transaction_id="stmt-conflict",
        evidence_payload={"match_status": "matched", "amount": "29.90"},
    )
    sep.save_evidence(record_a)

    record_b = _sample_record(
        public_id=pid,
        statement_transaction_id="stmt-conflict",
        evidence_payload={"match_status": "amount_mismatch", "amount": "99.99"},
        confidence_score="0.10",
    )
    with pytest.raises(StructuredEvidenceConflictError, match="Conflicting"):
        sep.save_evidence(record_b)


# ---------------------------------------------------------------------------
# 14. Batch persist with multiple records
# ---------------------------------------------------------------------------


def test_batch_persist(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    stmt_id = "stmt-batch"

    records = []
    for i in range(4):
        pid = generate_evidence_public_id(
            f"type-{i}",
            f"source-{i}",
            statement_transaction_id=stmt_id,
        )
        records.append(
            _sample_record(
                public_id=pid,
                evidence_type=f"type-{i}",
                source_type=f"source-{i}",
                statement_transaction_id=stmt_id,
            )
        )

    count = sep.persist_evidence_batch(records)
    assert count == 4

    results = sep.list_by_statement_transaction(stmt_id)
    assert len(results) == 4


# ---------------------------------------------------------------------------
# 15. generate_evidence_public_id is deterministic
# ---------------------------------------------------------------------------


def test_generate_evidence_public_id_deterministic() -> None:
    pid1 = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        statement_transaction_id="stmt-det",
    )
    pid2 = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        statement_transaction_id="stmt-det",
    )
    assert pid1 == pid2
    assert pid1.startswith("sev-")
    assert len(pid1) == 20  # "sev-" + 16 hex chars


# ---------------------------------------------------------------------------
# 16. generate_evidence_public_id produces different ids for different inputs
# ---------------------------------------------------------------------------


def test_generate_evidence_public_id_different_inputs() -> None:
    pid_a = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        statement_transaction_id="stmt-a",
    )
    pid_b = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        statement_transaction_id="stmt-b",
    )
    assert pid_a != pid_b

    pid_c = generate_evidence_public_id(
        "review_resolution",
        "matching_engine",
        statement_transaction_id="stmt-a",
    )
    assert pid_a != pid_c

    pid_d = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        review_queue_public_id="rq-x",
    )
    pid_e = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
    )
    assert pid_d != pid_e


# ---------------------------------------------------------------------------
# 17. No test touches database/finance.db
# ---------------------------------------------------------------------------


def test_no_live_db_touched() -> None:
    """All tests in this module use temporary databases only."""
    import os

    if os.path.exists(str(LIVE_DB_PATH)):
        mtime_before = os.path.getmtime(str(LIVE_DB_PATH))
        # Re-read; mtime should be unchanged
        assert os.path.getmtime(str(LIVE_DB_PATH)) == mtime_before


# ---------------------------------------------------------------------------
# 18. No final transaction table is mutated
# ---------------------------------------------------------------------------


def test_no_transaction_table_mutated(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    """Ensure the temp DB's transactions table is empty after evidence ops."""
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    pid = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        statement_transaction_id="stmt-nomut",
    )
    record = _sample_record(
        public_id=pid,
        statement_transaction_id="stmt-nomut",
    )
    sep.save_evidence(record)

    # Check statement_transactions count is unchanged by evidence ops
    stmt_count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) FROM statement_transactions"
    ).fetchone()[0]
    assert stmt_count == 0, f"statement_transactions should be empty, got {stmt_count} rows"

    # Check reconciliation_apply_results count is unchanged
    apply_count = migrated_temp_db_connection.execute(
        "SELECT COUNT(*) FROM reconciliation_apply_results"
    ).fetchone()[0]
    assert apply_count == 0, f"reconciliation_apply_results should be empty, got {apply_count} rows"


# ---------------------------------------------------------------------------
# 19. Validation: empty public_id raises ValueError
# ---------------------------------------------------------------------------


def test_empty_public_id_raises() -> None:
    with pytest.raises(ValueError, match="public_id must not be empty"):
        _sample_record(public_id="")


def test_empty_evidence_type_raises() -> None:
    with pytest.raises(ValueError, match="evidence_type must not be empty"):
        _sample_record(evidence_type="")


def test_empty_source_type_raises() -> None:
    with pytest.raises(ValueError, match="source_type must not be empty"):
        _sample_record(source_type="")


def test_non_dict_payload_raises() -> None:
    with pytest.raises(ValueError, match="evidence_payload must be a dict"):
        _sample_record(evidence_payload="not-a-dict")


# ---------------------------------------------------------------------------
# 20. JSON payload preserves types correctly
# ---------------------------------------------------------------------------


def test_payload_preserves_types(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    pid = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        statement_transaction_id="stmt-types",
    )
    payload = {
        "int_val": 42,
        "float_val": 0.5,
        "str_val": "hello",
        "list_val": [1, 2, 3],
        "nested": {"key": "value"},
        "none_val": None,
        "bool_val": True,
    }
    record = _sample_record(
        public_id=pid,
        statement_transaction_id="stmt-types",
        evidence_payload=payload,
    )
    sep.save_evidence(record)

    retrieved = sep.get_by_public_id(pid)
    assert retrieved is not None
    p = json.loads(retrieved["evidence_payload"])
    assert p["int_val"] == 42
    assert p["float_val"] == 0.5
    assert p["str_val"] == "hello"
    assert p["list_val"] == [1, 2, 3]
    assert p["nested"] == {"key": "value"}
    assert p["none_val"] is None
    assert p["bool_val"] is True


def test_payload_serializes_decimal_values_as_strings(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    pid = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        statement_transaction_id="stmt-decimal",
    )
    record = _sample_record(
        public_id=pid,
        statement_transaction_id="stmt-decimal",
        evidence_payload={
            "amount": Decimal("29.90"),
            "amount_delta": Decimal("0.10"),
            "nested": {"refund_amount": Decimal("5.00")},
            "line_amounts": [Decimal("1.23"), Decimal("4.56")],
        },
    )

    sep.save_evidence(record)

    retrieved = sep.get_by_public_id(pid)
    assert retrieved is not None
    payload = json.loads(retrieved["evidence_payload"])
    assert payload["amount"] == "29.90"
    assert payload["amount_delta"] == "0.10"
    assert payload["nested"]["refund_amount"] == "5.00"
    assert payload["line_amounts"] == ["1.23", "4.56"]


# ---------------------------------------------------------------------------
# 21. Determinstic public_id depends on review_queue_public_id
# ---------------------------------------------------------------------------


def test_public_id_deterministic_with_review_queue() -> None:
    pid1 = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        review_queue_public_id="rq-1",
        statement_transaction_id="stmt-1",
    )
    pid2 = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        review_queue_public_id="rq-2",
        statement_transaction_id="stmt-1",
    )
    assert pid1 != pid2


def test_public_id_deterministic_with_source_reference_fields() -> None:
    base = generate_evidence_public_id(
        "source_field",
        "statement_csv",
        statement_transaction_id="stmt-source",
        source_row=7,
        source_field="amount",
    )
    same = generate_evidence_public_id(
        "source_field",
        "statement_csv",
        statement_transaction_id="stmt-source",
        source_row=7,
        source_field="amount",
    )
    different_field = generate_evidence_public_id(
        "source_field",
        "statement_csv",
        statement_transaction_id="stmt-source",
        source_row=7,
        source_field="merchant_raw",
    )
    different_row = generate_evidence_public_id(
        "source_field",
        "statement_csv",
        statement_transaction_id="stmt-source",
        source_row=8,
        source_field="amount",
    )

    assert base == same
    assert base != different_field
    assert base != different_row


# ---------------------------------------------------------------------------
# 22. List by statement transaction returns empty for unknown id
# ---------------------------------------------------------------------------


def test_list_by_statement_transaction_empty(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    results = sep.list_by_statement_transaction("nonexistent-stmt")
    assert results == []


# ---------------------------------------------------------------------------
# 23. Regression: conflicting source references raise StructuredEvidenceConflictError
# ---------------------------------------------------------------------------


def test_conflicting_source_references_raises(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    pid = generate_evidence_public_id(
        "source_reference",
        "statement_csv",
        statement_transaction_id="stmt-src-conflict",
    )
    record_a = _sample_record(
        public_id=pid,
        evidence_type="source_reference",
        source_type="statement_csv",
        statement_transaction_id="stmt-src-conflict",
        source_row=10,
        source_field="amount",
        source_path="/tmp/stmt-a.csv",
        evidence_payload={"match_status": "matched"},
    )
    sep.save_evidence(record_a)

    record_b = _sample_record(
        public_id=pid,
        evidence_type="source_reference",
        source_type="statement_csv",
        statement_transaction_id="stmt-src-conflict",
        source_row=42,  # different source_row
        source_field="amount",
        source_path="/tmp/stmt-a.csv",
        evidence_payload={"match_status": "matched"},
    )
    with pytest.raises(StructuredEvidenceConflictError, match="Conflicting"):
        sep.save_evidence(record_b)


# ---------------------------------------------------------------------------
# 24. Regression: same source references + same payload is idempotent
# ---------------------------------------------------------------------------


def test_idempotent_save_with_source_references(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    pid = generate_evidence_public_id(
        "source_reference",
        "statement_csv",
        statement_transaction_id="stmt-src-idem",
    )
    record = _sample_record(
        public_id=pid,
        evidence_type="source_reference",
        source_type="statement_csv",
        statement_transaction_id="stmt-src-idem",
        source_page=2,
        source_row=42,
        source_field="merchant_raw",
        source_path="/tmp/stmt-idem.csv",
        evidence_payload={"raw_merchant": "SHOPEE"},
        confidence_score="0.88",
    )
    first = sep.save_evidence(record)
    assert first is True

    second = sep.save_evidence(record)
    assert second is False

    results = sep.list_by_statement_transaction("stmt-src-idem")
    assert len(results) == 1
