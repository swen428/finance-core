"""Tests for Calculation Audit Query Layer v1.

Covers get_run, get_snapshots, get_audit_trail, list_runs_for_entity,
and edge cases: missing run, run without snapshots, multiple snapshot
types, entity lookup.
Every test uses an independent in-memory SQLite database -- never touches
database/finance.db or live data.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from finance_core.calculation.audit_query import (
    CalculationAuditQueryService,
    CalculationAuditTrail,
)
from finance_core.calculation.run_persistence import (
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
    entity_type: str = "receipt_group",
    entity_id: str = "rg_test_001",
) -> str:
    rec = make_run_record(
        run_id=run_id or f"run_{uuid.uuid4()}",
        run_type="receipt_split",
        entity_type=entity_type,
        entity_id=entity_id,
        rule_version="v1",
        status="calculated",
        created_at=FROZEN_NOW,
    )
    repo = CalculationRunRepository(conn)
    repo.create(rec)
    conn.commit()
    return rec.run_id


def seed_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str | None = None,
    run_id: str,
    snapshot_type: str = "output_result",
    snapshot_data: str | None = None,
    created_at: str = FROZEN_NOW,
) -> CalculationSnapshotRecord:
    data = snapshot_data or json.dumps({"type": snapshot_type})
    snap = make_snapshot_record(
        snapshot_id=snapshot_id or f"snap_{uuid.uuid4()}",
        run_id=run_id,
        snapshot_type=snapshot_type,
        snapshot_data=data,
        created_at=created_at,
    )
    repo = CalculationSnapshotRepository(conn)
    repo.create(snap)
    conn.commit()
    return snap


def seed_full_trail(
    conn: sqlite3.Connection,
    *,
    run_id: str,
) -> None:
    """Seed one snapshot of each type for a run."""
    for stype in ("input_facts", "rules", "intermediate_values", "output_result"):
        seed_snapshot(
            conn,
            run_id=run_id,
            snapshot_type=stype,
            snapshot_data=json.dumps({"type": stype, "run_id": run_id}),
        )


def make_unvalidated_snapshot(
    *,
    snapshot_id: str,
    run_id: str,
    snapshot_type: str,
) -> CalculationSnapshotRecord:
    """Build a snapshot-like record for query-layer forward-compatibility tests.

    The current migration and persistence dataclass intentionally reject unknown
    snapshot types. This helper lets the audit-query DTO test future additive
    types without weakening persistence validation.
    """
    snap = object.__new__(CalculationSnapshotRecord)
    object.__setattr__(snap, "snapshot_id", snapshot_id)
    object.__setattr__(snap, "run_id", run_id)
    object.__setattr__(snap, "snapshot_type", snapshot_type)
    object.__setattr__(snap, "snapshot_data", json.dumps({"type": snapshot_type}))
    object.__setattr__(snap, "created_at", FROZEN_NOW)
    return snap


# -- 1. get_run --


def test_get_run_returns_record() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    run_id = seed_calc_run(conn)
    result = svc.get_run(run_id)

    assert result is not None
    assert result.run_id == run_id
    assert result.run_type == "receipt_split"
    assert result.status == "calculated"

    conn.close()


def test_get_run_missing_returns_none() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    result = svc.get_run("no_such_run")
    assert result is None

    conn.close()


# -- 2. get_snapshots --


def test_get_snapshots_returns_all_for_run() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    run_id = seed_calc_run(conn)
    snap1 = seed_snapshot(conn, run_id=run_id, snapshot_type="input_facts")
    snap2 = seed_snapshot(conn, run_id=run_id, snapshot_type="output_result")

    results = svc.get_snapshots(run_id)
    assert len(results) == 2
    ids = {r.snapshot_id for r in results}
    assert ids == {snap1.snapshot_id, snap2.snapshot_id}

    conn.close()


def test_get_snapshots_missing_run_returns_empty() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    results = svc.get_snapshots("no_such_run")
    assert results == ()

    conn.close()


def test_get_snapshots_run_without_snapshots_returns_empty() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    run_id = seed_calc_run(conn)
    results = svc.get_snapshots(run_id)
    assert results == ()

    conn.close()


# -- 3. get_audit_trail --


def test_get_audit_trail_full() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    run_id = seed_calc_run(conn, run_id="trail_full_run")
    seed_full_trail(conn, run_id=run_id)

    trail = svc.get_audit_trail(run_id)
    assert trail is not None
    assert trail.run.run_id == run_id
    assert len(trail.input_facts) == 1
    assert len(trail.rules) == 1
    assert len(trail.intermediate_values) == 1
    assert len(trail.output_result) == 1
    assert trail.has_snapshots is True
    assert trail.snapshot_count == 4

    conn.close()


def test_get_audit_trail_missing_run_returns_none() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    trail = svc.get_audit_trail("no_such_run")
    assert trail is None

    conn.close()


def test_get_audit_trail_run_without_snapshots() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    run_id = seed_calc_run(conn, run_id="no_snaps_run")
    trail = svc.get_audit_trail(run_id)

    assert trail is not None
    assert trail.run.run_id == run_id
    assert trail.input_facts == ()
    assert trail.rules == ()
    assert trail.intermediate_values == ()
    assert trail.output_result == ()
    assert trail.has_snapshots is False
    assert trail.snapshot_count == 0

    conn.close()


def test_get_audit_trail_multiple_same_type() -> None:
    """Multiple intermediate_values snapshots under one run."""
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    run_id = seed_calc_run(conn, run_id="multi_inter_run")
    seed_snapshot(
        conn,
        run_id=run_id,
        snapshot_type="intermediate_values",
        snapshot_data=json.dumps({"step": 1}),
    )
    seed_snapshot(
        conn,
        run_id=run_id,
        snapshot_type="intermediate_values",
        snapshot_data=json.dumps({"step": 2}),
    )
    seed_snapshot(
        conn,
        run_id=run_id,
        snapshot_type="output_result",
        snapshot_data=json.dumps({"result": "final"}),
    )

    trail = svc.get_audit_trail(run_id)
    assert trail is not None
    assert len(trail.intermediate_values) == 2
    assert len(trail.output_result) == 1
    assert trail.input_facts == ()
    assert trail.rules == ()
    assert trail.snapshot_count == 3

    conn.close()


def test_get_audit_trail_is_frozen() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    run_id = seed_calc_run(conn, run_id="frozen_trail_run")
    seed_full_trail(conn, run_id=run_id)

    trail = svc.get_audit_trail(run_id)
    assert trail is not None

    with pytest.raises(Exception):
        trail.run = "mutated"  # type: ignore[misc]

    conn.close()


def test_get_audit_trail_all_snapshots_property() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    run_id = seed_calc_run(conn, run_id="all_snaps_run")
    seed_snapshot(conn, run_id=run_id, snapshot_type="input_facts")
    seed_snapshot(conn, run_id=run_id, snapshot_type="output_result")

    trail = svc.get_audit_trail(run_id)
    assert trail is not None
    all_snaps = trail.all_snapshots
    assert len(all_snaps) == 2
    types = {s.snapshot_type for s in all_snaps}
    assert types == {"input_facts", "output_result"}

    conn.close()


def test_audit_trail_preserves_unknown_snapshot_types() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    run_id = seed_calc_run(conn, run_id="unknown_snapshot_run")
    run = svc.get_run(run_id)
    assert run is not None

    known = seed_snapshot(conn, run_id=run_id, snapshot_type="output_result")
    unknown = make_unvalidated_snapshot(
        snapshot_id="snap_future_type",
        run_id=run_id,
        snapshot_type="future_additive_snapshot",
    )

    trail = CalculationAuditTrail(
        run=run,
        output_result=(known,),
        unknown_snapshots=(unknown,),
    )

    assert trail.output_result == (known,)
    assert trail.unknown_snapshots == (unknown,)
    assert trail.all_snapshots == (known, unknown)
    assert trail.snapshot_count == 2

    conn.close()


# -- 4. list_runs_for_entity --


def test_list_runs_for_entity_returns_runs() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    run_id_1 = seed_calc_run(conn, entity_type="receipt_group", entity_id="rg_001")
    run_id_2 = seed_calc_run(conn, entity_type="receipt_group", entity_id="rg_001")

    results = svc.list_runs_for_entity("receipt_group", "rg_001")
    assert len(results) == 2
    ids = {r.run_id for r in results}
    assert ids == {run_id_1, run_id_2}

    conn.close()


def test_list_runs_for_entity_empty_returns_empty() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    results = svc.list_runs_for_entity("receipt_group", "no_such_entity")
    assert results == ()

    conn.close()


def test_list_runs_for_entity_isolates_by_type() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    run_a = seed_calc_run(conn, entity_type="receipt_group", entity_id="rg_iso")
    run_b = seed_calc_run(conn, entity_type="receipt", entity_id="rec_iso")

    results_rg = svc.list_runs_for_entity("receipt_group", "rg_iso")
    results_rec = svc.list_runs_for_entity("receipt", "rec_iso")

    assert len(results_rg) == 1
    assert results_rg[0].run_id == run_a
    assert len(results_rec) == 1
    assert results_rec[0].run_id == run_b

    conn.close()


def test_list_runs_for_entity_respects_limit() -> None:
    conn = make_migrated_connection()
    svc = CalculationAuditQueryService(conn)

    for _ in range(5):
        seed_calc_run(conn, entity_type="receipt_group", entity_id="rg_limit")

    results = svc.list_runs_for_entity("receipt_group", "rg_limit", limit=3)
    assert len(results) == 3

    conn.close()
