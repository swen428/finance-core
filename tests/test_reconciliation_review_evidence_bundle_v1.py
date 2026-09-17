"""Tests for Reconciliation Review Evidence Bundle v1.

Covers:
  1. Successful bundle build for a review queue item with evidence
  2. Evidence collected by review_queue_public_id
  3. Evidence collected by statement_transaction_id
  4. Evidence collected by app_transaction_id
  5. Duplicate evidence deduplication
  6. Deterministic evidence ordering (single-path)
  7. Global stable ordering across collection paths
  8. Explanation input includes evidence public IDs
  9. Raw payload preservation
 10. Parsed payload included when valid
 11. Malformed payload produces warning but does not destroy raw payload
 12. Missing evidence returns empty evidence with warning
 13. Missing review item returns None
 14. No mutation: row counts before and after bundle generation unchanged
 15. ReviewEvidenceBundle fields populated correctly
 16. ExplanationInput facts and warnings populated correctly
 17. Evidence isolation across review items
 18. Conflicting evidence types warning
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from finance_core.reconciliation.review_evidence import (
    ExplanationInput,
    ReviewEvidenceBundle,
    ReviewEvidenceService,
)
from finance_core.reconciliation.structured_evidence import (
    EvidenceQueryResult,
    EvidenceReader,
    StructuredEvidencePersistence,
    StructuredEvidenceRecord,
    generate_evidence_public_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_review_queue_row(
    conn: sqlite3.Connection,
    *,
    public_id: str,
    run_public_id: str = "run-test-001",
    candidate_id: str = "cand-001",
    issue_type: str = "amount_mismatch",
    suggested_action: str = "adjust_app_transaction",
    priority: int = 1,
    statement_transaction_ref: str | None = None,
    app_transaction_ref: str | None = None,
    status: str = "pending",
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_review_queue (
            public_id, run_public_id, candidate_id, issue_type,
            suggested_action, priority,
            statement_transaction_ref, app_transaction_ref,
            confidence_score, reason_codes_json, evidence_json, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            public_id,
            run_public_id,
            candidate_id,
            issue_type,
            suggested_action,
            priority,
            statement_transaction_ref,
            app_transaction_ref,
            "0.85",
            json.dumps(["amount_differs"]),
            json.dumps({"match_status": "amount_mismatch"}),
            status,
        ),
    )
    conn.commit()


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
            "match_status": "amount_mismatch",
            "amount": "29.90",
            "currency": "SGD",
        },
        confidence_score="0.95",
        review_queue_public_id="rq-test-001",
        statement_transaction_id="stmt-test-001",
        app_transaction_id="app-test-001",
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


def _build_service(
    conn: sqlite3.Connection,
) -> ReviewEvidenceService:
    return ReviewEvidenceService(
        conn=conn,
        evidence_reader=EvidenceReader(StructuredEvidencePersistence(conn)),
    )


def _count_evidence_rows(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS cnt FROM reconciliation_structured_evidence").fetchone()
    return row["cnt"] if row else 0


def _count_review_queue_rows(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS cnt FROM reconciliation_review_queue").fetchone()
    return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# 1. Successful bundle build for a review queue item with evidence
# ---------------------------------------------------------------------------


def test_build_bundle_success(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(
        conn,
        public_id="rq-test-001",
        statement_transaction_ref="stmt-test-001",
        app_transaction_ref="app-test-001",
    )
    _persist_sample(conn, review_queue_public_id="rq-test-001")

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-001")

    assert bundle is not None
    assert isinstance(bundle, ReviewEvidenceBundle)
    assert bundle.review_queue_public_id == "rq-test-001"
    assert bundle.statement_transaction_id == "stmt-test-001"
    assert bundle.app_transaction_id == "app-test-001"
    assert len(bundle.evidence) == 1
    assert all(isinstance(e, EvidenceQueryResult) for e in bundle.evidence)


# ---------------------------------------------------------------------------
# 2. Evidence collected by review_queue_public_id
# ---------------------------------------------------------------------------


def test_evidence_collected_by_review_queue_public_id(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(conn, public_id="rq-test-002")

    public_id = generate_evidence_public_id(
        "matching_decision",
        "matching_engine",
        statement_transaction_id="stmt-test-002",
    )
    sep, _ = _persist_sample(
        conn,
        public_id=public_id,
        review_queue_public_id="rq-test-002",
    )

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-002")

    assert bundle is not None
    assert len(bundle.evidence) == 1
    assert bundle.evidence[0].public_id == public_id


# ---------------------------------------------------------------------------
# 3. Evidence collected by statement_transaction_id
# ---------------------------------------------------------------------------


def test_evidence_collected_by_statement_transaction_id(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(
        conn,
        public_id="rq-test-003",
        statement_transaction_ref="stmt-unique-003",
    )

    public_id = generate_evidence_public_id(
        "statement_transaction",
        "statement_csv",
        statement_transaction_id="stmt-unique-003",
    )
    _persist_sample(
        conn,
        public_id=public_id,
        evidence_type="statement_transaction",
        source_type="statement_csv",
        review_queue_public_id=None,
        statement_transaction_id="stmt-unique-003",
        app_transaction_id=None,
    )

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-003")

    assert bundle is not None
    assert len(bundle.evidence) == 1
    assert bundle.evidence[0].public_id == public_id
    assert bundle.evidence[0].statement_transaction_id == "stmt-unique-003"


# ---------------------------------------------------------------------------
# 4. Evidence collected by app_transaction_id
# ---------------------------------------------------------------------------


def test_evidence_collected_by_app_transaction_id(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(
        conn,
        public_id="rq-test-004",
        app_transaction_ref="app-unique-004",
    )

    public_id = generate_evidence_public_id(
        "app_transaction",
        "matching_engine",
        app_transaction_id="app-unique-004",
    )
    _persist_sample(
        conn,
        public_id=public_id,
        evidence_type="app_transaction",
        source_type="matching_engine",
        review_queue_public_id=None,
        statement_transaction_id=None,
        app_transaction_id="app-unique-004",
    )

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-004")

    assert bundle is not None
    assert len(bundle.evidence) == 1
    assert bundle.evidence[0].app_transaction_id == "app-unique-004"


# ---------------------------------------------------------------------------
# 5. Duplicate evidence deduplication
# ---------------------------------------------------------------------------


def test_duplicate_evidence_deduplication(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    shared_public_id = "sev-shared-001"

    _insert_review_queue_row(
        conn,
        public_id="rq-test-005",
        statement_transaction_ref="stmt-shared",
        app_transaction_ref="app-shared",
    )

    record = StructuredEvidenceRecord(
        public_id=shared_public_id,
        evidence_type="matching_decision",
        source_type="matching_engine",
        evidence_payload={"match_status": "amount_mismatch"},
        review_queue_public_id="rq-test-005",
        statement_transaction_id="stmt-shared",
        app_transaction_id="app-shared",
    )
    sep = StructuredEvidencePersistence(conn)
    sep.save_evidence(record)

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-005")

    assert bundle is not None
    public_ids = [e.public_id for e in bundle.evidence]
    assert public_ids.count(shared_public_id) == 1
    assert len(bundle.evidence) == 1


# ---------------------------------------------------------------------------
# 6. Deterministic evidence ordering
# ---------------------------------------------------------------------------


def test_deterministic_evidence_ordering(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(conn, public_id="rq-test-006")

    sep = StructuredEvidencePersistence(conn)
    records = [
        _sample_record(
            public_id="sev-order-001",
            review_queue_public_id="rq-test-006",
            evidence_type="matching_decision",
        ),
        _sample_record(
            public_id="sev-order-002",
            review_queue_public_id="rq-test-006",
            evidence_type="statement_transaction",
        ),
        _sample_record(
            public_id="sev-order-003",
            review_queue_public_id="rq-test-006",
            evidence_type="review_resolution",
        ),
    ]
    for r in records:
        sep.save_evidence(r)

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-006")

    assert bundle is not None
    assert len(bundle.evidence) == 3
    ids = [e.id for e in bundle.evidence]
    assert ids == sorted(ids)

    bundle2 = service.build_bundle("rq-test-006")
    assert bundle2 is not None
    assert [e.public_id for e in bundle.evidence] == [e.public_id for e in bundle2.evidence]


# ---------------------------------------------------------------------------
# 7. Global stable ordering across collection paths
# ---------------------------------------------------------------------------


def test_global_ordering_across_collection_paths(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(
        conn,
        public_id="rq-test-global-order",
        statement_transaction_ref="stmt-global",
        app_transaction_ref="app-global",
    )

    # Insert evidence discovered via different keys with carefully
    # chosen created_at values so that path-collection order (review_queue
    # first, then statement, then app) differs from global created_at order.
    conn.execute(
        """
        INSERT INTO reconciliation_structured_evidence (
            public_id, review_queue_public_id, evidence_type, source_type,
            confidence_score, evidence_payload, created_at, updated_at
        ) VALUES
        ('sev-global-001', 'rq-test-global-order',
         'matching_decision', 'matching_engine', '0.95',
         '{"x":1}','2026-01-03 10:00:00','2026-01-03 10:00:00'),
        ('sev-global-002', NULL,
         'statement_transaction', 'statement_csv', '0.95',
         '{"x":2}','2026-01-01 10:00:00','2026-01-01 10:00:00'),
        ('sev-global-003', NULL,
         'app_transaction', 'matching_engine', '0.95',
         '{"x":3}','2026-01-02 10:00:00','2026-01-02 10:00:00')
        """
    )
    # Link the two path-only records to their respective keys.
    conn.execute(
        """UPDATE reconciliation_structured_evidence
        SET statement_transaction_id = 'stmt-global'
        WHERE public_id = 'sev-global-002'"""
    )
    conn.execute(
        """UPDATE reconciliation_structured_evidence
        SET app_transaction_id = 'app-global'
        WHERE public_id = 'sev-global-003'"""
    )
    conn.commit()

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-global-order")
    assert bundle is not None
    assert len(bundle.evidence) == 3

    ids = [e.public_id for e in bundle.evidence]
    # Global created_at order: sev-global-002 (2026-01-01),
    # sev-global-003 (2026-01-02), sev-global-001 (2026-01-03).
    assert ids == ["sev-global-002", "sev-global-003", "sev-global-001"]


# ---------------------------------------------------------------------------
# 8. Explanation input includes evidence public IDs
# ---------------------------------------------------------------------------


def test_explanation_input_includes_evidence_public_ids(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(conn, public_id="rq-test-007")

    public_ids = {
        generate_evidence_public_id(
            "matching_decision",
            "matching_engine",
            statement_transaction_id=f"stmt-test-00{i}",
        )
        for i in range(7, 10)
    }
    sep = StructuredEvidencePersistence(conn)
    for pid in public_ids:
        rec = _sample_record(public_id=pid, review_queue_public_id="rq-test-007")
        sep.save_evidence(rec)

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-007")

    assert bundle is not None
    ei = bundle.explanation_input
    assert isinstance(ei, ExplanationInput)
    assert ei.review_queue_public_id == "rq-test-007"
    assert set(ei.evidence_public_ids) == public_ids
    assert len(ei.evidence_public_ids) == 3


# ---------------------------------------------------------------------------
# 9. Raw payload preservation
# ---------------------------------------------------------------------------


def test_raw_payload_preservation(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(conn, public_id="rq-test-008")

    payload = {"match_status": "matched", "amount": "42.00", "currency": "SGD"}
    record = _sample_record(
        public_id=generate_evidence_public_id(
            "matching_decision",
            "matching_engine",
            statement_transaction_id="stmt-raw-001",
        ),
        evidence_payload=payload,
        review_queue_public_id="rq-test-008",
    )
    sep = StructuredEvidencePersistence(conn)
    sep.save_evidence(record)

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-008")

    assert bundle is not None
    assert len(bundle.evidence) == 1
    ev = bundle.evidence[0]
    assert isinstance(ev.evidence_payload, str)
    parsed_payload = json.loads(ev.evidence_payload)
    assert parsed_payload == payload


# ---------------------------------------------------------------------------
# 10. Parsed payload included when valid
# ---------------------------------------------------------------------------


def test_parsed_payload_included_when_valid(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(conn, public_id="rq-test-009")

    record = _sample_record(
        public_id=generate_evidence_public_id(
            "review_resolution",
            "review_queue",
            review_queue_public_id="rq-test-009",
        ),
        evidence_payload={"resolution": "confirm_match", "reviewer": "human"},
        review_queue_public_id="rq-test-009",
    )
    sep = StructuredEvidencePersistence(conn)
    sep.save_evidence(record)

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-009")

    assert bundle is not None
    ev = bundle.evidence[0]
    parsed = ev.parsed_payload()
    assert isinstance(parsed, dict)
    assert parsed["resolution"] == "confirm_match"
    assert parsed["reviewer"] == "human"


# ---------------------------------------------------------------------------
# 11. Malformed payload produces warning but does not destroy raw payload
# ---------------------------------------------------------------------------


def test_malformed_payload_warning_raw_preserved(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(conn, public_id="rq-test-010")

    malformed_payload = '{"bad_json": '
    conn.execute(
        """
        INSERT INTO reconciliation_structured_evidence (
            public_id, review_queue_public_id,
            evidence_type, source_type,
            confidence_score, evidence_payload,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            "sev-malformed-001",
            "rq-test-010",
            "matching_decision",
            "matching_engine",
            "0.85",
            malformed_payload,
        ),
    )
    conn.commit()

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-010")

    assert bundle is not None
    assert len(bundle.evidence) == 1
    ev = bundle.evidence[0]

    assert ev.evidence_payload == malformed_payload
    assert ev.parsed_payload_or_none() is None

    with pytest.raises(ValueError):
        ev.parsed_payload()

    ei = bundle.explanation_input
    malformed_warnings = [w for w in ei.warnings if w.startswith("malformed_evidence_payload")]
    assert len(malformed_warnings) == 1
    assert "sev-malformed-001" in malformed_warnings[0]


# ---------------------------------------------------------------------------
# 12. Missing evidence returns empty evidence with warning
# ---------------------------------------------------------------------------


def test_missing_evidence_empty_list_with_warning(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(conn, public_id="rq-test-011")

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-011")

    assert bundle is not None
    assert len(bundle.evidence) == 0
    assert "no_structured_evidence_found" in bundle.explanation_input.warnings


# ---------------------------------------------------------------------------
# 13. Missing review item returns None
# ---------------------------------------------------------------------------


def test_missing_review_item_returns_none(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    service = _build_service(conn)
    bundle = service.build_bundle("rq-nonexistent")
    assert bundle is None


# ---------------------------------------------------------------------------
# 14. No mutation: row counts unchanged after bundle generation
# ---------------------------------------------------------------------------


def test_no_mutation_row_counts_unchanged(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(
        conn,
        public_id="rq-test-012",
        statement_transaction_ref="stmt-immutable",
    )
    _persist_sample(
        conn,
        public_id=generate_evidence_public_id(
            "matching_decision",
            "matching_engine",
            statement_transaction_id="stmt-immutable",
        ),
        review_queue_public_id="rq-test-012",
        statement_transaction_id="stmt-immutable",
    )

    evidence_before = _count_evidence_rows(conn)
    review_before = _count_review_queue_rows(conn)

    service = _build_service(conn)
    for _ in range(3):
        bundle = service.build_bundle("rq-test-012")
        assert bundle is not None

    evidence_after = _count_evidence_rows(conn)
    review_after = _count_review_queue_rows(conn)

    assert evidence_before == evidence_after
    assert review_before == review_after


# ---------------------------------------------------------------------------
# 15. ReviewEvidenceBundle fields populated correctly
# ---------------------------------------------------------------------------


def test_bundle_fields_populated_correctly(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(
        conn,
        public_id="rq-test-013",
        statement_transaction_ref="stmt-fld",
        app_transaction_ref="app-fld",
        status="pending",
    )
    _persist_sample(
        conn,
        public_id=generate_evidence_public_id(
            "matching_decision",
            "matching_engine",
            statement_transaction_id="stmt-fld",
        ),
        review_queue_public_id="rq-test-013",
        statement_transaction_id="stmt-fld",
    )

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-013")

    assert bundle is not None
    assert bundle.review_queue_public_id == "rq-test-013"
    assert bundle.statement_transaction_id == "stmt-fld"
    assert bundle.app_transaction_id == "app-fld"
    assert bundle.match_status is None
    assert len(bundle.evidence) == 1


# ---------------------------------------------------------------------------
# 16. ExplanationInput facts and warnings populated correctly
# ---------------------------------------------------------------------------


def test_explanation_input_facts_and_warnings(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(
        conn,
        public_id="rq-test-014",
        statement_transaction_ref=None,
        app_transaction_ref=None,
    )
    _persist_sample(
        conn,
        public_id=generate_evidence_public_id(
            "matching_decision",
            "matching_engine",
            statement_transaction_id="stmt-facts",
        ),
        review_queue_public_id="rq-test-014",
    )

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-014")

    assert bundle is not None
    ei = bundle.explanation_input

    fact_texts = ei.facts
    assert any("total_evidence_records: 1" in f for f in fact_texts)
    assert any("evidence_type_matching_decision" in f for f in fact_texts)

    assert "missing_statement_transaction_id" in ei.warnings
    assert "missing_app_transaction_id" in ei.warnings

    assert len(ei.summary) > 0
    assert "rq-test-014" in ei.summary


# ---------------------------------------------------------------------------
# 17. Evidence across multiple review queue items is isolated
# ---------------------------------------------------------------------------


def test_evidence_isolation_across_review_items(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(conn, public_id="rq-isolated-a")
    _insert_review_queue_row(conn, public_id="rq-isolated-b")

    sep = StructuredEvidencePersistence(conn)
    rec_a = _sample_record(
        public_id=generate_evidence_public_id(
            "matching_decision",
            "matching_engine",
            review_queue_public_id="rq-isolated-a",
        ),
        review_queue_public_id="rq-isolated-a",
    )
    rec_b = _sample_record(
        public_id=generate_evidence_public_id(
            "matching_decision",
            "matching_engine",
            review_queue_public_id="rq-isolated-b",
        ),
        review_queue_public_id="rq-isolated-b",
    )
    sep.save_evidence(rec_a)
    sep.save_evidence(rec_b)

    service = _build_service(conn)
    bundle_a = service.build_bundle("rq-isolated-a")
    bundle_b = service.build_bundle("rq-isolated-b")

    assert bundle_a is not None
    assert bundle_b is not None
    assert len(bundle_a.evidence) == 1
    assert len(bundle_b.evidence) == 1
    assert bundle_a.evidence[0].public_id != bundle_b.evidence[0].public_id


# ---------------------------------------------------------------------------
# 18. Conflicting evidence types warning
# ---------------------------------------------------------------------------


def test_conflicting_evidence_types_warning(
    migrated_temp_db_connection: sqlite3.Connection,
) -> None:
    conn = migrated_temp_db_connection
    _insert_review_queue_row(conn, public_id="rq-test-conflict")

    sep = StructuredEvidencePersistence(conn)
    sep.save_evidence(
        _sample_record(
            public_id=generate_evidence_public_id(
                "matching_decision",
                "matching_engine",
                review_queue_public_id="rq-test-conflict",
            ),
            evidence_type="matching_decision",
            review_queue_public_id="rq-test-conflict",
        )
    )
    sep.save_evidence(
        _sample_record(
            public_id=generate_evidence_public_id(
                "review_resolution",
                "review_queue",
                review_queue_public_id="rq-test-conflict",
            ),
            evidence_type="review_resolution",
            review_queue_public_id="rq-test-conflict",
        )
    )

    service = _build_service(conn)
    bundle = service.build_bundle("rq-test-conflict")

    assert bundle is not None
    assert any(
        w.startswith("conflicting_evidence_types") for w in bundle.explanation_input.warnings
    )
