"""Tests for Calculation Run Persistence v1.

Covers schema creation, insert, fetch, list, and invalid-value rejection.
Every test uses an independent in-memory SQLite database -- never touches
database/finance.db or live data.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from finance_core.calculation.run_persistence import (
    CalculationRunRecord,
    CalculationRunRepository,
    make_run_record,
)
from finance_core.resources import migrations_dir

MIGRATION_PATH = migrations_dir() / "015_calculation_run_persistence.sql"

FROZEN_NOW = "2026-06-18T12:00:00+08:00"


# -- helpers --


def create_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def apply_migration(conn: sqlite3.Connection) -> None:
    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    conn.executescript(migration_sql)


def make_migrated_connection() -> sqlite3.Connection:
    conn = create_connection()
    apply_migration(conn)
    return conn


def sample_record(
    *,
    run_id: str | None = None,
    run_type: str = "receipt_split",
    entity_type: str = "receipt_group",
    entity_id: str = "rg_test_001",
    rule_version: str = "v1",
    status: str = "calculated",
    **kwargs: object,
) -> CalculationRunRecord:
    created_at_val: str = str(kwargs.pop("created_at", FROZEN_NOW))
    return make_run_record(
        run_id=run_id or f"run_{uuid.uuid4()}",
        run_type=run_type,
        entity_type=entity_type,
        entity_id=entity_id,
        rule_version=rule_version,
        status=status,
        created_at=created_at_val,
        **{k: v for k, v in kwargs.items() if v is not None},  # type: ignore[misc]
    )


# -- 1. Schema creation --


def test_schema_creates_table() -> None:
    conn = make_migrated_connection()
    repo = CalculationRunRepository(conn)

    rec = sample_record(
        run_id="schema_test_run",
        source_type="test",
        source_reference="ref_001",
    )
    repo.create(rec)
    conn.commit()

    fetched = repo.fetch_by_run_id("schema_test_run")
    assert fetched is not None
    assert fetched.run_id == "schema_test_run"
    assert fetched.run_type == "receipt_split"
    assert fetched.entity_type == "receipt_group"
    assert fetched.entity_id == "rg_test_001"
    assert fetched.rule_version == "v1"
    assert fetched.status == "calculated"
    assert fetched.source_type == "test"
    assert fetched.source_reference == "ref_001"
    assert fetched.created_at == FROZEN_NOW

    conn.close()


# -- 2. Insert run --


def test_insert_run() -> None:
    conn = make_migrated_connection()
    repo = CalculationRunRepository(conn)

    rec = sample_record(run_id="insert_test_run")
    repo.create(rec)
    conn.commit()

    fetched = repo.fetch_by_run_id("insert_test_run")
    assert fetched is not None
    assert fetched.run_id == "insert_test_run"
    assert fetched.status == "calculated"

    conn.close()


def test_insert_run_with_optional_fields_null() -> None:
    conn = make_migrated_connection()
    repo = CalculationRunRepository(conn)

    rec = sample_record(
        run_id="null_opt_test",
        source_type=None,
        source_reference=None,
    )
    repo.create(rec)
    conn.commit()

    fetched = repo.fetch_by_run_id("null_opt_test")
    assert fetched is not None
    assert fetched.source_type is None
    assert fetched.source_reference is None

    conn.close()


def test_insert_duplicate_run_id_rejected() -> None:
    conn = make_migrated_connection()
    repo = CalculationRunRepository(conn)

    rec = sample_record(run_id="dup_test_run")
    repo.create(rec)
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        repo.create(rec)

    conn.close()


# -- 3. Fetch run --


def test_fetch_by_run_id_missing_returns_none() -> None:
    conn = make_migrated_connection()
    repo = CalculationRunRepository(conn)

    result = repo.fetch_by_run_id("no_such_run")
    assert result is None

    conn.close()


def test_fetch_by_run_id_returns_all_fields() -> None:
    conn = make_migrated_connection()
    repo = CalculationRunRepository(conn)

    rec = sample_record(
        run_id="full_field_test",
        run_type="settlement",
        entity_type="receipt_group",
        entity_id="rg_full_001",
        rule_version="settlement_v2",
        status="pending",
        source_type="receipt_finalization",
        source_reference="ref_full_001",
    )
    repo.create(rec)
    conn.commit()

    fetched = repo.fetch_by_run_id("full_field_test")
    assert fetched is not None
    assert fetched.run_id == "full_field_test"
    assert fetched.run_type == "settlement"
    assert fetched.entity_type == "receipt_group"
    assert fetched.entity_id == "rg_full_001"
    assert fetched.rule_version == "settlement_v2"
    assert fetched.status == "pending"
    assert fetched.source_type == "receipt_finalization"
    assert fetched.source_reference == "ref_full_001"
    assert fetched.created_at == FROZEN_NOW

    conn.close()


# -- 4. List runs --


def test_list_by_entity_returns_runs_newest_first() -> None:
    conn = make_migrated_connection()
    repo = CalculationRunRepository(conn)

    rec1 = sample_record(
        run_id="list_test_1",
        entity_type="receipt_group",
        entity_id="rg_list_001",
        created_at="2026-06-17T10:00:00+08:00",
    )
    rec2 = sample_record(
        run_id="list_test_2",
        entity_type="receipt_group",
        entity_id="rg_list_001",
        created_at="2026-06-18T10:00:00+08:00",
    )
    repo.create(rec1)
    repo.create(rec2)
    conn.commit()

    results = repo.list_by_entity("receipt_group", "rg_list_001")

    assert len(results) == 2
    assert results[0].run_id == "list_test_2"
    assert results[1].run_id == "list_test_1"

    conn.close()


def test_list_by_entity_empty_returns_empty_tuple() -> None:
    conn = make_migrated_connection()
    repo = CalculationRunRepository(conn)

    results = repo.list_by_entity("receipt_group", "no_such_entity")
    assert results == ()

    conn.close()


def test_list_by_entity_respects_limit() -> None:
    conn = make_migrated_connection()
    repo = CalculationRunRepository(conn)

    for i in range(5):
        rec = sample_record(
            run_id=f"limit_test_{i}",
            entity_type="receipt_group",
            entity_id="rg_limit_001",
            created_at=f"2026-06-1{i}T10:00:00+08:00",
        )
        repo.create(rec)
    conn.commit()

    results = repo.list_by_entity("receipt_group", "rg_limit_001", limit=3)
    assert len(results) == 3

    conn.close()


def test_list_by_entity_different_entities_isolated() -> None:
    conn = make_migrated_connection()
    repo = CalculationRunRepository(conn)

    rec_a = sample_record(
        run_id="iso_test_a",
        entity_type="receipt_group",
        entity_id="rg_iso",
    )
    rec_b = sample_record(
        run_id="iso_test_b",
        entity_type="receipt",
        entity_id="rec_iso",
    )
    repo.create(rec_a)
    repo.create(rec_b)
    conn.commit()

    results_a = repo.list_by_entity("receipt_group", "rg_iso")
    results_b = repo.list_by_entity("receipt", "rec_iso")

    assert len(results_a) == 1
    assert results_a[0].run_id == "iso_test_a"
    assert len(results_b) == 1
    assert results_b[0].run_id == "iso_test_b"

    conn.close()


# -- 5. Invalid status rejected --


def test_invalid_status_rejected_by_validation() -> None:
    with pytest.raises(ValueError, match="Invalid status"):
        make_run_record(
            run_id="bad_status",
            run_type="receipt_split",
            entity_type="receipt_group",
            entity_id="rg_001",
            rule_version="v1",
            status="invalid_status_value",
        )


def test_invalid_status_rejected_by_schema() -> None:
    conn = make_migrated_connection()

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            """\
            INSERT INTO calc_audit_runs (
                run_id, run_type, entity_type, entity_id,
                rule_version, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "schema_reject_status",
                "receipt_split",
                "receipt_group",
                "rg_001",
                "v1",
                "invalid_status_value",
                FROZEN_NOW,
            ),
        )

    conn.close()


# -- 6. Invalid run_type rejected --


def test_invalid_run_type_rejected_by_validation() -> None:
    with pytest.raises(ValueError, match="Invalid run_type"):
        make_run_record(
            run_id="bad_run_type",
            run_type="invalid_run_type_value",
            entity_type="receipt_group",
            entity_id="rg_001",
            rule_version="v1",
            status="calculated",
        )


def test_invalid_run_type_rejected_by_schema() -> None:
    conn = make_migrated_connection()

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            """\
            INSERT INTO calc_audit_runs (
                run_id, run_type, entity_type, entity_id,
                rule_version, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "schema_reject_run_type",
                "invalid_run_type_value",
                "receipt_group",
                "rg_001",
                "v1",
                "calculated",
                FROZEN_NOW,
            ),
        )

    conn.close()


# -- 7. Invalid entity_type rejected --


def test_invalid_entity_type_rejected_by_validation() -> None:
    with pytest.raises(ValueError, match="Invalid entity_type"):
        make_run_record(
            run_id="bad_entity_type",
            run_type="receipt_split",
            entity_type="invalid_entity_type_value",
            entity_id="rg_001",
            rule_version="v1",
            status="calculated",
        )


def test_invalid_entity_type_rejected_by_schema() -> None:
    conn = make_migrated_connection()

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            """\
            INSERT INTO calc_audit_runs (
                run_id, run_type, entity_type, entity_id,
                rule_version, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "schema_reject_entity_type",
                "receipt_split",
                "invalid_entity_type_value",
                "rg_001",
                "v1",
                "calculated",
                FROZEN_NOW,
            ),
        )

    conn.close()


# -- 8. Required field guards --


def test_missing_run_id_raises() -> None:
    with pytest.raises(ValueError, match="run_id is required"):
        CalculationRunRecord(
            run_id="",
            run_type="receipt_split",
            entity_type="receipt_group",
            entity_id="rg_001",
            rule_version="v1",
            status="calculated",
            created_at=FROZEN_NOW,
        )


def test_missing_created_at_raises() -> None:
    with pytest.raises(ValueError, match="created_at is required"):
        CalculationRunRecord(
            run_id="no_ts",
            run_type="receipt_split",
            entity_type="receipt_group",
            entity_id="rg_001",
            rule_version="v1",
            status="calculated",
            created_at="",
        )


# -- 9. Immutability --


def test_record_is_frozen() -> None:
    rec = sample_record(run_id="frozen_test")
    with pytest.raises(Exception):
        rec.run_id = "mutated"  # type: ignore[misc]


# -- 10. make_run_record timestamp injection --


def test_make_run_record_uses_provided_timestamp() -> None:
    rec = make_run_record(
        run_id="ts_test",
        run_type="receipt_split",
        entity_type="receipt_group",
        entity_id="rg_001",
        rule_version="v1",
        status="calculated",
        created_at=FROZEN_NOW,
    )
    assert rec.created_at == FROZEN_NOW


def test_make_run_record_auto_generates_timestamp() -> None:
    rec = make_run_record(
        run_id="auto_ts_test",
        run_type="receipt_split",
        entity_type="receipt_group",
        entity_id="rg_001",
        rule_version="v1",
    )
    assert rec.created_at
    assert "T" in rec.created_at
    assert rec.status == "pending"
