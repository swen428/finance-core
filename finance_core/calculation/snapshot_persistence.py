"""Calculation Snapshot Persistence v1 -- repository for calculation_snapshots.

Provides ``CalculationSnapshotRecord`` (immutable dataclass) and
``CalculationSnapshotRepository`` (caller-supplied ``sqlite3.Connection`` only).
This module never opens database files, creates default paths, interprets
snapshot JSON, or performs calculations.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

# -- valid enum set (mirrored from CHECK constraint in
#    016_calculation_snapshot_persistence.sql) --

VALID_SNAPSHOT_TYPES: frozenset[str] = frozenset(
    {
        "input_facts",
        "rules",
        "intermediate_values",
        "output_result",
    }
)


# -- domain model --


@dataclass(frozen=True)
class CalculationSnapshotRecord:
    """Immutable record of a single calculation content snapshot.

    Mirrors the ``calculation_snapshots`` table schema.  Validation is
    performed on construction so invalid records are never created.
    """

    snapshot_id: str
    run_id: str
    snapshot_type: str
    snapshot_data: str
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            raise ValueError("snapshot_id is required")
        if not self.run_id:
            raise ValueError("run_id is required")
        if not self.snapshot_type:
            raise ValueError("snapshot_type is required")
        if self.snapshot_type not in VALID_SNAPSHOT_TYPES:
            raise ValueError(
                f"Invalid snapshot_type {self.snapshot_type!r}; "
                f"must be one of {sorted(VALID_SNAPSHOT_TYPES)}"
            )
        if not self.snapshot_data:
            raise ValueError("snapshot_data is required")
        if not self.created_at:
            raise ValueError("created_at is required")


# -- repository --


class CalculationSnapshotRepository:
    """Repository for ``calculation_snapshots`` persistence.

    Accepts a caller-supplied ``sqlite3.Connection`` only.  Does not
    open database files, create default paths, interpret snapshot JSON,
    or perform calculations.  The caller is responsible for connection
    lifecycle, migration, and commit/rollback decisions.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(self, record: CalculationSnapshotRecord) -> None:
        """Insert a new calculation snapshot record.

        Raises ``sqlite3.IntegrityError`` if a snapshot with the same
        ``snapshot_id`` already exists (UNIQUE PRIMARY KEY constraint),
        or if ``run_id`` does not reference a valid row in
        ``calc_audit_runs`` (FOREIGN KEY constraint).
        """
        self._conn.execute(
            """\
            INSERT INTO calculation_snapshots (
                snapshot_id,
                run_id,
                snapshot_type,
                snapshot_data,
                created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                record.snapshot_id,
                record.run_id,
                record.snapshot_type,
                record.snapshot_data,
                record.created_at,
            ),
        )

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    def fetch_by_snapshot_id(self, snapshot_id: str) -> CalculationSnapshotRecord | None:
        """Return the record for *snapshot_id*, or ``None`` if not found."""
        row = self._conn.execute(
            "SELECT * FROM calculation_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_snapshot_record(row)

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    def list_by_run_id(self, run_id: str) -> Sequence[CalculationSnapshotRecord]:
        """Return all snapshots for a calculation run, ordered by
        ``created_at`` ascending."""
        rows = self._conn.execute(
            """\
            SELECT * FROM calculation_snapshots
            WHERE run_id = ?
            ORDER BY created_at ASC
            """,
            (run_id,),
        ).fetchall()
        return tuple(_row_to_snapshot_record(r) for r in rows)

    def list_by_run_id_and_type(
        self, run_id: str, snapshot_type: str
    ) -> Sequence[CalculationSnapshotRecord]:
        """Return all snapshots of *snapshot_type* for a calculation run,
        ordered by ``created_at`` ascending."""
        rows = self._conn.execute(
            """\
            SELECT * FROM calculation_snapshots
            WHERE run_id = ? AND snapshot_type = ?
            ORDER BY created_at ASC
            """,
            (run_id, snapshot_type),
        ).fetchall()
        return tuple(_row_to_snapshot_record(r) for r in rows)


# -- helpers --


def _row_to_snapshot_record(row: Any) -> CalculationSnapshotRecord:
    if not isinstance(row, sqlite3.Row):
        _columns = [
            "snapshot_id",
            "run_id",
            "snapshot_type",
            "snapshot_data",
            "created_at",
        ]
        row = dict(zip(_columns, row))
    else:
        row = dict(row)

    return CalculationSnapshotRecord(
        snapshot_id=row["snapshot_id"],
        run_id=row["run_id"],
        snapshot_type=row["snapshot_type"],
        snapshot_data=row["snapshot_data"],
        created_at=row["created_at"],
    )


# -- public factory --


def make_snapshot_record(
    *,
    snapshot_id: str,
    run_id: str,
    snapshot_type: str,
    snapshot_data: str,
    created_at: str | None = None,
) -> CalculationSnapshotRecord:
    """Create a validated ``CalculationSnapshotRecord`` with an optional
    ``created_at`` timestamp.  When ``created_at`` is ``None``, the
    current UTC time is used.
    """
    ts = created_at or datetime.now(timezone.utc).isoformat()
    return CalculationSnapshotRecord(
        snapshot_id=snapshot_id,
        run_id=run_id,
        snapshot_type=snapshot_type,
        snapshot_data=snapshot_data,
        created_at=ts,
    )


__all__ = [
    "CalculationSnapshotRecord",
    "CalculationSnapshotRepository",
    "make_snapshot_record",
    "VALID_SNAPSHOT_TYPES",
]
