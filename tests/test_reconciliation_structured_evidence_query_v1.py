"""Tests for Reconciliation Structured Evidence Read/Query Layer v1.

Covers:
  1. EvidenceReader.get_by_id returns typed EvidenceQueryResult
  2. EvidenceReader.get_by_id returns None for missing id
  3. EvidenceReader.get_by_public_id returns typed result
  4. EvidenceReader.get_by_public_id returns None for missing public_id
  5. EvidenceReader.list_by_review_queue with stable ordering
  6. EvidenceReader.list_by_statement_transaction with stable ordering
  7. EvidenceReader.list_by_app_transaction with stable ordering
  8. EvidenceReader.list_by_evidence_type with stable ordering
  9. EvidenceReader.list_all with stable ordering
 10. EvidenceReader returns empty list for no matches
 11. EvidenceQueryResult preserves raw evidence_payload exactly
 12. EvidenceQueryResult.parsed_payload round-trips
 13. EvidenceQueryResult.parsed_payload raises ValueError for malformed JSON
 14. EvidenceQueryResult.parsed_payload_or_none returns None for malformed JSON
 15. EvidenceQueryResult.raw payload remains accessible when JSON is malformed
 16. EvidenceReader does not mutate evidence rows
 17. No test touches database/finance.db
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from finance_core.reconciliation.structured_evidence import (
    EvidenceQueryResult,
    EvidenceReader,
    StructuredEvidencePersistence,
    StructuredEvidenceRecord,
    generate_evidence_public_id,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB_PATH = REPO_ROOT / "database" / "finance.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_record(**kw) -> StructuredEvidenceRecord:
    defaults: dict = dict(
        public_id=generate_evidence_public_id(
            "matching_decision",
            "matching_engine",
            statement_transaction_id="stmt-test-reader",
        ),
        evidence_type="matching_decision",
        source_type="matching_engine",
        evidence_payload={
            "match_status": "matched",
            "amount": "29.90",
            "currency": "SGD",
        },
        confidence_score="0.95",
        statement_transaction_id="stmt-test-reader",
    )
    defaults.update(kw)
    return StructuredEvidenceRecord(**defaults)


def _persist_sample(
    conn: sqlite3.Connection,
    **kw,
) -> tuple[StructuredEvidencePersistence, StructuredEvidenceRecord]:
    sep = StructuredEvidencePersistence(conn)
    record = _sample_record(**kw)
    sep.save_evidence(record)
    return sep, record


# ---------------------------------------------------------------------------
# 1. EvidenceReader.get_by_id returns typed EvidenceQueryResult
# ---------------------------------------------------------------------------


def test_get_by_id_returns_typed_result(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep, record = _persist_sample(migrated_temp_db_connection)
    reader = EvidenceReader(sep)

    # Retrieve the row we just inserted to get its id
    raw = sep.get_by_public_id(record.public_id)
    assert raw is not None
    evidence_id = raw["id"]

    result = reader.get_by_id(evidence_id)
    assert result is not None
    assert isinstance(result, EvidenceQueryResult)
    assert result.id == evidence_id
    assert result.public_id == record.public_id
    assert result.evidence_type == record.evidence_type
    assert result.source_type == record.source_type
    assert result.confidence_score == record.confidence_score
    assert result.statement_transaction_id == record.statement_transaction_id


# ---------------------------------------------------------------------------
# 2. EvidenceReader.get_by_id returns None for missing id
# ---------------------------------------------------------------------------


def test_get_by_id_returns_none_for_missing(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    reader = EvidenceReader(sep)

    result = reader.get_by_id(99999)
    assert result is None


# ---------------------------------------------------------------------------
# 3. EvidenceReader.get_by_public_id returns typed result
# ---------------------------------------------------------------------------


def test_get_by_public_id_returns_typed_result(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep, record = _persist_sample(migrated_temp_db_connection)
    reader = EvidenceReader(sep)

    result = reader.get_by_public_id(record.public_id)
    assert result is not None
    assert isinstance(result, EvidenceQueryResult)
    assert result.public_id == record.public_id
    assert result.evidence_type == "matching_decision"
    assert result.source_type == "matching_engine"
    assert result.statement_transaction_id == "stmt-test-reader"


# ---------------------------------------------------------------------------
# 4. EvidenceReader.get_by_public_id returns None for missing
# ---------------------------------------------------------------------------


def test_get_by_public_id_returns_none_for_missing(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    reader = EvidenceReader(sep)

    result = reader.get_by_public_id("sev-nonexistent")
    assert result is None


# ---------------------------------------------------------------------------
# 5. EvidenceReader.list_by_review_queue with stable ordering
# ---------------------------------------------------------------------------


def test_list_by_review_queue_typed_ordered(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    reader = EvidenceReader(sep)
    rq_id = "rq-query-test"

    for i in range(3):
        pid = generate_evidence_public_id(
            f"type-{i}",
            f"source-{i}",
            review_queue_public_id=rq_id,
            statement_transaction_id=f"stmt-{i}",
        )
        sep.save_evidence(
            _sample_record(
                public_id=pid,
                review_queue_public_id=rq_id,
                statement_transaction_id=f"stmt-{i}",
                evidence_type=f"type-{i}",
                source_type=f"source-{i}",
            )
        )

    results = reader.list_by_review_queue(rq_id)
    assert len(results) == 3
    assert all(isinstance(r, EvidenceQueryResult) for r in results)
    assert all(r.review_queue_public_id == rq_id for r in results)

    # Stable ordering: created_at ASC, id ASC
    for i in range(len(results) - 1):
        assert results[i].created_at <= results[i + 1].created_at
        if results[i].created_at == results[i + 1].created_at:
            assert results[i].id < results[i + 1].id


# ---------------------------------------------------------------------------
# 6. EvidenceReader.list_by_statement_transaction with stable ordering
# ---------------------------------------------------------------------------


def test_list_by_statement_transaction_typed_ordered(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    reader = EvidenceReader(sep)
    stmt_id = "stmt-query-ordered"

    for i in range(3):
        pid = generate_evidence_public_id(
            f"type-{i}",
            f"source-{i}",
            statement_transaction_id=stmt_id,
        )
        sep.save_evidence(
            _sample_record(
                public_id=pid,
                evidence_type=f"type-{i}",
                source_type=f"source-{i}",
                statement_transaction_id=stmt_id,
            )
        )

    results = reader.list_by_statement_transaction(stmt_id)
    assert len(results) == 3
    assert all(isinstance(r, EvidenceQueryResult) for r in results)
    assert all(r.statement_transaction_id == stmt_id for r in results)

    for i in range(len(results) - 1):
        assert results[i].created_at <= results[i + 1].created_at
        if results[i].created_at == results[i + 1].created_at:
            assert results[i].id < results[i + 1].id


# ---------------------------------------------------------------------------
# 7. EvidenceReader.list_by_app_transaction with stable ordering
# ---------------------------------------------------------------------------


def test_list_by_app_transaction_typed_ordered(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    reader = EvidenceReader(sep)
    app_id = "app-query-ordered"

    for i in range(2):
        pid = generate_evidence_public_id(
            f"type-{i}",
            f"source-{i}",
            statement_transaction_id=f"stmt-app-{i}",
            app_transaction_id=app_id,
        )
        sep.save_evidence(
            _sample_record(
                public_id=pid,
                evidence_type=f"type-{i}",
                source_type=f"source-{i}",
                statement_transaction_id=f"stmt-app-{i}",
                app_transaction_id=app_id,
            )
        )

    results = reader.list_by_app_transaction(app_id)
    assert len(results) == 2
    assert all(isinstance(r, EvidenceQueryResult) for r in results)
    assert all(r.app_transaction_id == app_id for r in results)

    for i in range(len(results) - 1):
        assert results[i].created_at <= results[i + 1].created_at
        if results[i].created_at == results[i + 1].created_at:
            assert results[i].id < results[i + 1].id


# ---------------------------------------------------------------------------
# 8. EvidenceReader.list_by_evidence_type with stable ordering
# ---------------------------------------------------------------------------


def test_list_by_evidence_type_typed_ordered(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    reader = EvidenceReader(sep)
    etype = "matching_decision"

    for i in range(4):
        pid = generate_evidence_public_id(
            etype,
            f"source-{i}",
            statement_transaction_id=f"stmt-{i}",
        )
        sep.save_evidence(
            _sample_record(
                public_id=pid,
                evidence_type=etype,
                source_type=f"source-{i}",
                statement_transaction_id=f"stmt-{i}",
            )
        )

    results = reader.list_by_evidence_type(etype)
    assert len(results) == 4
    assert all(isinstance(r, EvidenceQueryResult) for r in results)
    assert all(r.evidence_type == etype for r in results)

    for i in range(len(results) - 1):
        assert results[i].created_at <= results[i + 1].created_at
        if results[i].created_at == results[i + 1].created_at:
            assert results[i].id < results[i + 1].id


# ---------------------------------------------------------------------------
# 9. EvidenceReader.list_all with stable ordering
# ---------------------------------------------------------------------------


def test_list_all_typed_ordered(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    reader = EvidenceReader(sep)

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

    results = reader.list_all()
    assert len(results) == 5
    assert all(isinstance(r, EvidenceQueryResult) for r in results)

    for i in range(len(results) - 1):
        assert results[i].created_at <= results[i + 1].created_at
        if results[i].created_at == results[i + 1].created_at:
            assert results[i].id < results[i + 1].id


# ---------------------------------------------------------------------------
# 10. Empty query returns empty list
# ---------------------------------------------------------------------------


def test_empty_query_returns_empty_list(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    reader = EvidenceReader(sep)

    assert reader.list_by_review_queue("nonexistent-rq") == []
    assert reader.list_by_statement_transaction("nonexistent-stmt") == []
    assert reader.list_by_app_transaction("nonexistent-app") == []
    assert reader.list_by_evidence_type("nonexistent-type") == []
    assert reader.list_all() == []


# ---------------------------------------------------------------------------
# 11. EvidenceQueryResult preserves raw evidence_payload exactly
# ---------------------------------------------------------------------------


def test_raw_payload_preserved_exactly(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep, record = _persist_sample(
        migrated_temp_db_connection,
        evidence_payload={"match_status": "matched", "amount": "29.90"},
    )
    reader = EvidenceReader(sep)
    result = reader.get_by_public_id(record.public_id)
    assert result is not None

    # Raw payload text is accessible as a string
    assert isinstance(result.evidence_payload, str)
    # It should be a valid JSON string
    parsed = json.loads(result.evidence_payload)
    assert parsed["match_status"] == "matched"
    assert parsed["amount"] == "29.90"


# ---------------------------------------------------------------------------
# 12. EvidenceQueryResult.parsed_payload round-trips
# ---------------------------------------------------------------------------


def test_parsed_payload_round_trip(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    original_payload = {
        "match_status": "amount_mismatch",
        "amount": "99.99",
        "currency": "SGD",
        "reason_codes": ["AMOUNT_MISMATCH"],
        "nested": {"confidence": "0.85"},
    }
    sep, record = _persist_sample(
        migrated_temp_db_connection,
        evidence_payload=original_payload,
    )
    reader = EvidenceReader(sep)
    result = reader.get_by_public_id(record.public_id)
    assert result is not None

    parsed = result.parsed_payload()
    assert parsed == original_payload
    assert parsed["match_status"] == "amount_mismatch"
    assert parsed["amount"] == "99.99"
    assert parsed["nested"]["confidence"] == "0.85"


# ---------------------------------------------------------------------------
# 13. parsed_payload raises ValueError for malformed JSON
# ---------------------------------------------------------------------------


def test_parsed_payload_raises_for_malformed_json(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    # Insert a row with malformed JSON directly via SQL, bypassing the
    # persistence layer (which always produces valid JSON).
    public_id = "sev-malformed-json-test"
    migrated_temp_db_connection.execute(
        """
        INSERT INTO reconciliation_structured_evidence
            (public_id, evidence_type, source_type, evidence_payload,
             confidence_score, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            public_id,
            "matching_decision",
            "matching_engine",
            "{this is not valid json !!!",
            "0.0",
            "2026-06-12T00:00:00",
            "2026-06-12T00:00:00",
        ),
    )
    migrated_temp_db_connection.commit()

    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    reader = EvidenceReader(sep)
    result = reader.get_by_public_id(public_id)
    assert result is not None

    with pytest.raises(ValueError, match="Malformed evidence_payload JSON"):
        result.parsed_payload()


# ---------------------------------------------------------------------------
# 14. parsed_payload_or_none returns None for malformed JSON
# ---------------------------------------------------------------------------


def test_parsed_payload_or_none_returns_none_for_malformed(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    public_id = "sev-malformed-or-none"
    migrated_temp_db_connection.execute(
        """
        INSERT INTO reconciliation_structured_evidence
            (public_id, evidence_type, source_type, evidence_payload,
             confidence_score, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            public_id,
            "matching_decision",
            "matching_engine",
            "{{broken",
            "0.0",
            "2026-06-12T00:00:00",
            "2026-06-12T00:00:00",
        ),
    )
    migrated_temp_db_connection.commit()

    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    reader = EvidenceReader(sep)
    result = reader.get_by_public_id(public_id)
    assert result is not None

    assert result.parsed_payload_or_none() is None


# ---------------------------------------------------------------------------
# 15. Raw payload remains accessible when JSON is malformed
# ---------------------------------------------------------------------------


def test_raw_payload_accessible_despite_malformed_json(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    public_id = "sev-raw-accessible"
    bad_json = "{this is definitely not json!!!"
    migrated_temp_db_connection.execute(
        """
        INSERT INTO reconciliation_structured_evidence
            (public_id, evidence_type, source_type, evidence_payload,
             confidence_score, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            public_id,
            "matching_decision",
            "matching_engine",
            bad_json,
            "0.0",
            "2026-06-12T00:00:00",
            "2026-06-12T00:00:00",
        ),
    )
    migrated_temp_db_connection.commit()

    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    reader = EvidenceReader(sep)
    result = reader.get_by_public_id(public_id)
    assert result is not None

    # Raw payload is exactly what was stored
    assert result.evidence_payload == bad_json

    # But parsed_payload raises
    with pytest.raises(ValueError):
        result.parsed_payload()

    # And parsed_payload_or_none returns None
    assert result.parsed_payload_or_none() is None


# ---------------------------------------------------------------------------
# 16. EvidenceReader does not mutate evidence rows
# ---------------------------------------------------------------------------


def test_reader_does_not_mutate_evidence(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    sep, record = _persist_sample(migrated_temp_db_connection)
    reader = EvidenceReader(sep)

    raw_before = sep.get_by_public_id(record.public_id)
    assert raw_before is not None

    # Exercise all read paths on the reader
    reader.get_by_id(raw_before["id"])
    reader.get_by_public_id(record.public_id)
    reader.list_by_review_queue("nonexistent-rq")
    reader.list_by_statement_transaction(record.statement_transaction_id or "")
    reader.list_all()

    # Verify the stored row is unchanged
    raw_after = sep.get_by_public_id(record.public_id)
    assert raw_after is not None
    assert raw_after == raw_before

    # Verify no new rows were created by read operations
    count = sep._conn.execute("SELECT COUNT(*) FROM reconciliation_structured_evidence").fetchone()[
        0
    ]
    assert count == 1


# ---------------------------------------------------------------------------
# 17. parsed_payload raises ValueError when JSON decodes to non-dict
# ---------------------------------------------------------------------------


def test_parsed_payload_raises_for_non_dict_json(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    public_id = "sev-non-dict"
    migrated_temp_db_connection.execute(
        """
        INSERT INTO reconciliation_structured_evidence
            (public_id, evidence_type, source_type, evidence_payload,
             confidence_score, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            public_id,
            "matching_decision",
            "matching_engine",
            "[1, 2, 3]",
            "0.0",
            "2026-06-12T00:00:00",
            "2026-06-12T00:00:00",
        ),
    )
    migrated_temp_db_connection.commit()

    sep = StructuredEvidencePersistence(migrated_temp_db_connection)
    reader = EvidenceReader(sep)
    result = reader.get_by_public_id(public_id)
    assert result is not None

    with pytest.raises(ValueError, match="expected dict"):
        result.parsed_payload()
