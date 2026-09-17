"""Tests for Calculation Snapshot Persistence v1.

Covers schema creation, insert, fetch, list, FK enforcement, invalid
snapshot_type rejection, and multiple snapshots under one run.
Every test uses an independent in-memory SQLite database -- never touches
database/finance.db or live data.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from finance_core.calculation.run_persistence import (
    CalculationRunRecord,
    CalculationRunRepository,
    make_run_record,
)
from finance_core.calculation.snapshot_persistence import (
    CalculationSnapshotRecord,
    CalculationSnapshotRepository,
    make_snapshot_record,
)
from finance_core.resources import migrations_dir

MIGRATION_PATH_RUN = migrations_dir() / "015_calculation_run_persistence.sql"
MIGRATION_PATH_SNAPSHOT = migrations_dir() / "016_calculation_snapshot_persistence.sql"

FROZEN_NOW = "2026-06-18T12:00:00+08:00"


# -- helpers --


def create_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


def apply_migration(conn: sqlite3.Connection, path: Path) -> None:
    migration_sql = path.read_text(encoding="utf-8")
    conn.executescript(migration_sql)


def make_migrated_connection() -> sqlite3.Connection:
    conn = create_connection()
    apply_migration(conn, MIGRATION_PATH_RUN)
    apply_migration(conn, MIGRATION_PATH_SNAPSHOT)
    return conn


def seed_calc_run(
    conn: sqlite3.Connection,
    *,
    run_id: str | None = None,
) -> CalculationRunRecord:
    rec = make_run_record(
        run_id=run_id or f"run_{uuid.uuid4()}",
        run_type="receipt_split",
        entity_type="receipt_group",
        entity_id="rg_test_001",
        rule_version="v1",
        status="calculated",
        created_at=FROZEN_NOW,
    )
    repo = CalculationRunRepository(conn)
    repo.create(rec)
    return rec


def sample_record(
    *,
    snapshot_id: str | None = None,
    run_id: str = "run_test_001",
    snapshot_type: str = "output_result",
    snapshot_data: str | None = None,
    created_at: str = FROZEN_NOW,
) -> CalculationSnapshotRecord:
    data = snapshot_data or json.dumps({"total": "100.00", "currency": "SGD"})
    return make_snapshot_record(
        snapshot_id=snapshot_id or f"snap_{uuid.uuid4()}",
        run_id=run_id,
        snapshot_type=snapshot_type,
        snapshot_data=data,
        created_at=created_at,
    )


# -- 1. Schema creation --


def test_schema_creates_table() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    run_rec = seed_calc_run(conn, run_id="schema_run_001")

    snap = sample_record(
        snapshot_id="schema_snap_001",
        run_id=run_rec.run_id,
        snapshot_type="output_result",
        snapshot_data=json.dumps({"result": "ok"}),
    )
    repo.create(snap)
    conn.commit()

    fetched = repo.fetch_by_snapshot_id("schema_snap_001")
    assert fetched is not None
    assert fetched.snapshot_id == "schema_snap_001"
    assert fetched.run_id == run_rec.run_id
    assert fetched.snapshot_type == "output_result"
    assert fetched.snapshot_data == json.dumps({"result": "ok"})
    assert fetched.created_at == FROZEN_NOW

    conn.close()


def test_schema_creates_all_snapshot_types() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    run_rec = seed_calc_run(conn, run_id="all_types_run")

    types = ["input_facts", "rules", "intermediate_values", "output_result"]
    for i, stype in enumerate(types):
        snap = sample_record(
            snapshot_id=f"all_types_snap_{i}",
            run_id=run_rec.run_id,
            snapshot_type=stype,
        )
        repo.create(snap)

    conn.commit()

    results = repo.list_by_run_id(run_rec.run_id)
    assert len(results) == 4
    stored_types = {r.snapshot_type for r in results}
    assert stored_types == set(types)

    conn.close()


# -- 2. Insert snapshot --


def test_insert_snapshot() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    run_rec = seed_calc_run(conn, run_id="insert_run_001")

    data = json.dumps({"inputs": {"case_id": "CASE_001"}})
    snap = sample_record(
        snapshot_id="insert_snap_001",
        run_id=run_rec.run_id,
        snapshot_type="input_facts",
        snapshot_data=data,
    )
    repo.create(snap)
    conn.commit()

    fetched = repo.fetch_by_snapshot_id("insert_snap_001")
    assert fetched is not None
    assert fetched.snapshot_id == "insert_snap_001"
    assert fetched.run_id == run_rec.run_id
    assert fetched.snapshot_type == "input_facts"
    assert fetched.snapshot_data == data

    conn.close()


def test_insert_duplicate_snapshot_id_rejected() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    run_rec = seed_calc_run(conn, run_id="dup_run_001")

    snap = sample_record(
        snapshot_id="dup_snap_001",
        run_id=run_rec.run_id,
    )
    repo.create(snap)
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        repo.create(snap)

    conn.close()


# -- 3. Fetch snapshot --


def test_fetch_by_snapshot_id_missing_returns_none() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    result = repo.fetch_by_snapshot_id("no_such_snapshot")
    assert result is None

    conn.close()


def test_fetch_by_snapshot_id_returns_all_fields() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    run_rec = seed_calc_run(conn, run_id="full_fields_run")

    data = json.dumps(
        {
            "participant_shares": {"Owner": "13.55", "MemberA": "14.91"},
            "total_to_collect": "56.57",
        }
    )
    snap = sample_record(
        snapshot_id="full_fields_snap",
        run_id=run_rec.run_id,
        snapshot_type="output_result",
        snapshot_data=data,
    )
    repo.create(snap)
    conn.commit()

    fetched = repo.fetch_by_snapshot_id("full_fields_snap")
    assert fetched is not None
    assert fetched.snapshot_id == "full_fields_snap"
    assert fetched.run_id == run_rec.run_id
    assert fetched.snapshot_type == "output_result"
    assert fetched.snapshot_data == data
    assert fetched.created_at == FROZEN_NOW

    conn.close()


# -- 4. List by run --


def test_list_by_run_id_returns_all_snapshots_ordered() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    run_rec = seed_calc_run(conn, run_id="list_run_001")

    snap1 = sample_record(
        snapshot_id="list_snap_1",
        run_id=run_rec.run_id,
        snapshot_type="input_facts",
        created_at="2026-06-18T10:00:00+08:00",
    )
    snap2 = sample_record(
        snapshot_id="list_snap_2",
        run_id=run_rec.run_id,
        snapshot_type="rules",
        created_at="2026-06-18T11:00:00+08:00",
    )
    snap3 = sample_record(
        snapshot_id="list_snap_3",
        run_id=run_rec.run_id,
        snapshot_type="output_result",
        created_at="2026-06-18T12:00:00+08:00",
    )
    repo.create(snap1)
    repo.create(snap2)
    repo.create(snap3)
    conn.commit()

    results = repo.list_by_run_id(run_rec.run_id)
    assert len(results) == 3
    # Ordered ascending by created_at
    assert results[0].snapshot_id == "list_snap_1"
    assert results[1].snapshot_id == "list_snap_2"
    assert results[2].snapshot_id == "list_snap_3"

    conn.close()


def test_list_by_run_id_empty_returns_empty_tuple() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    results = repo.list_by_run_id("no_such_run")
    assert results == ()

    conn.close()


def test_list_by_run_id_isolates_runs() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    run_a = seed_calc_run(conn, run_id="iso_run_A")
    run_b = seed_calc_run(conn, run_id="iso_run_B")

    snap_a = sample_record(
        snapshot_id="iso_snap_A",
        run_id=run_a.run_id,
        snapshot_type="output_result",
    )
    snap_b = sample_record(
        snapshot_id="iso_snap_B",
        run_id=run_b.run_id,
        snapshot_type="output_result",
    )
    repo.create(snap_a)
    repo.create(snap_b)
    conn.commit()

    results_a = repo.list_by_run_id(run_a.run_id)
    results_b = repo.list_by_run_id(run_b.run_id)

    assert len(results_a) == 1
    assert results_a[0].snapshot_id == "iso_snap_A"
    assert len(results_b) == 1
    assert results_b[0].snapshot_id == "iso_snap_B"

    conn.close()


# -- 5. List by run and type --


def test_list_by_run_id_and_type_filters_correctly() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    run_rec = seed_calc_run(conn, run_id="filter_run_001")

    snap_input = sample_record(
        snapshot_id="filter_snap_input",
        run_id=run_rec.run_id,
        snapshot_type="input_facts",
    )
    snap_rules = sample_record(
        snapshot_id="filter_snap_rules",
        run_id=run_rec.run_id,
        snapshot_type="rules",
    )
    snap_output = sample_record(
        snapshot_id="filter_snap_output",
        run_id=run_rec.run_id,
        snapshot_type="output_result",
    )
    repo.create(snap_input)
    repo.create(snap_rules)
    repo.create(snap_output)
    conn.commit()

    rules_results = repo.list_by_run_id_and_type(run_rec.run_id, "rules")
    assert len(rules_results) == 1
    assert rules_results[0].snapshot_id == "filter_snap_rules"

    output_results = repo.list_by_run_id_and_type(run_rec.run_id, "output_result")
    assert len(output_results) == 1
    assert output_results[0].snapshot_id == "filter_snap_output"

    conn.close()


def test_list_by_run_id_and_type_empty_returns_empty_tuple() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    seed_calc_run(conn, run_id="empty_type_run")

    results = repo.list_by_run_id_and_type("empty_type_run", "output_result")
    assert results == ()

    conn.close()


def test_list_by_run_id_and_type_multiple_same_type() -> None:
    """Multiple snapshots of the same type under one run should all be returned."""
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    run_rec = seed_calc_run(conn, run_id="multi_type_run")

    snap1 = sample_record(
        snapshot_id="multi_inter_1",
        run_id=run_rec.run_id,
        snapshot_type="intermediate_values",
        created_at="2026-06-18T10:00:00+08:00",
        snapshot_data=json.dumps({"step": 1}),
    )
    snap2 = sample_record(
        snapshot_id="multi_inter_2",
        run_id=run_rec.run_id,
        snapshot_type="intermediate_values",
        created_at="2026-06-18T11:00:00+08:00",
        snapshot_data=json.dumps({"step": 2}),
    )
    repo.create(snap1)
    repo.create(snap2)
    conn.commit()

    results = repo.list_by_run_id_and_type(run_rec.run_id, "intermediate_values")
    assert len(results) == 2
    assert results[0].snapshot_id == "multi_inter_1"
    assert results[1].snapshot_id == "multi_inter_2"

    conn.close()


# -- 6. FK enforcement --


def test_fk_rejects_orphan_run_id() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    snap = sample_record(
        snapshot_id="orphan_snap",
        run_id="no_such_run_id",
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.create(snap)

    conn.close()


def test_fk_allows_snapshot_when_run_exists() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    run_rec = seed_calc_run(conn, run_id="valid_fk_run")

    snap = sample_record(
        snapshot_id="valid_fk_snap",
        run_id=run_rec.run_id,
    )
    repo.create(snap)
    conn.commit()

    fetched = repo.fetch_by_snapshot_id("valid_fk_snap")
    assert fetched is not None

    conn.close()


# -- 7. Invalid snapshot_type rejection --


def test_invalid_snapshot_type_rejected_by_validation() -> None:
    with pytest.raises(ValueError, match="Invalid snapshot_type"):
        make_snapshot_record(
            snapshot_id="bad_type_snap",
            run_id="run_001",
            snapshot_type="invalid_type_value",
            snapshot_data=json.dumps({}),
        )


def test_invalid_snapshot_type_rejected_by_schema() -> None:
    conn = make_migrated_connection()
    seed_calc_run(conn, run_id="schema_reject_run")

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        conn.execute(
            """\
            INSERT INTO calculation_snapshots (
                snapshot_id, run_id, snapshot_type, snapshot_data, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "schema_reject_snap",
                "schema_reject_run",
                "invalid_type_value",
                json.dumps({}),
                FROZEN_NOW,
            ),
        )

    conn.close()


# -- 8. Required field guards --


def test_missing_snapshot_id_raises() -> None:
    with pytest.raises(ValueError, match="snapshot_id is required"):
        CalculationSnapshotRecord(
            snapshot_id="",
            run_id="run_001",
            snapshot_type="output_result",
            snapshot_data=json.dumps({}),
            created_at=FROZEN_NOW,
        )


def test_missing_run_id_raises() -> None:
    with pytest.raises(ValueError, match="run_id is required"):
        CalculationSnapshotRecord(
            snapshot_id="snap_001",
            run_id="",
            snapshot_type="output_result",
            snapshot_data=json.dumps({}),
            created_at=FROZEN_NOW,
        )


def test_missing_snapshot_data_raises() -> None:
    with pytest.raises(ValueError, match="snapshot_data is required"):
        CalculationSnapshotRecord(
            snapshot_id="snap_001",
            run_id="run_001",
            snapshot_type="output_result",
            snapshot_data="",
            created_at=FROZEN_NOW,
        )


def test_missing_created_at_raises() -> None:
    with pytest.raises(ValueError, match="created_at is required"):
        CalculationSnapshotRecord(
            snapshot_id="snap_001",
            run_id="run_001",
            snapshot_type="output_result",
            snapshot_data=json.dumps({}),
            created_at="",
        )


# -- 9. Immutability --


def test_record_is_frozen() -> None:
    snap = sample_record(snapshot_id="frozen_snap")
    with pytest.raises(Exception):
        snap.snapshot_id = "mutated"  # type: ignore[misc]


# -- 10. make_snapshot_record timestamp injection --


def test_make_snapshot_record_uses_provided_timestamp() -> None:
    snap = make_snapshot_record(
        snapshot_id="ts_snap",
        run_id="run_001",
        snapshot_type="output_result",
        snapshot_data=json.dumps({}),
        created_at=FROZEN_NOW,
    )
    assert snap.created_at == FROZEN_NOW
    assert snap.snapshot_type == "output_result"


def test_make_snapshot_record_auto_generates_timestamp() -> None:
    snap = make_snapshot_record(
        snapshot_id="auto_ts_snap",
        run_id="run_001",
        snapshot_type="output_result",
        snapshot_data=json.dumps({}),
    )
    assert snap.created_at
    assert "T" in snap.created_at


# -- 11. Multiple snapshots under one run --


def test_multiple_snapshots_under_one_run() -> None:
    conn = make_migrated_connection()
    repo = CalculationSnapshotRepository(conn)

    run_rec = seed_calc_run(conn, run_id="multi_snap_run")

    snap1 = sample_record(
        snapshot_id="multi_1",
        run_id=run_rec.run_id,
        snapshot_type="input_facts",
    )
    snap2 = sample_record(
        snapshot_id="multi_2",
        run_id=run_rec.run_id,
        snapshot_type="rules",
    )
    snap3 = sample_record(
        snapshot_id="multi_3",
        run_id=run_rec.run_id,
        snapshot_type="intermediate_values",
    )
    snap4 = sample_record(
        snapshot_id="multi_4",
        run_id=run_rec.run_id,
        snapshot_type="output_result",
    )
    repo.create(snap1)
    repo.create(snap2)
    repo.create(snap3)
    repo.create(snap4)
    conn.commit()

    results = repo.list_by_run_id(run_rec.run_id)
    assert len(results) == 4
    ids = {r.snapshot_id for r in results}
    assert ids == {"multi_1", "multi_2", "multi_3", "multi_4"}

    # Verify each type accessible via list_by_run_id_and_type
    for stype in ("input_facts", "rules", "intermediate_values", "output_result"):
        typed = repo.list_by_run_id_and_type(run_rec.run_id, stype)
        assert len(typed) == 1
        assert typed[0].snapshot_type == stype

    conn.close()
