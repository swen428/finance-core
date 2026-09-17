"""Calculation Run Persistence v1 -- repository for calc_audit_runs.

Provides ``CalculationRunRecord`` (immutable dataclass) and
``CalculationRunRepository`` (caller-supplied ``sqlite3.Connection`` only).
This module never opens database files, creates default paths, or performs
calculations.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

# -- valid enum sets (mirrored from CHECK constraints in
#    015_calculation_run_persistence.sql) --

VALID_RUN_TYPES: frozenset[str] = frozenset(
    {
        "receipt_split",
        "settlement",
        "reconciliation",
        "parser_proposal",
        "confirmation",
        "finalization",
        "audit_snapshot",
    }
)

VALID_ENTITY_TYPES: frozenset[str] = frozenset(
    {
        "receipt_group",
        "receipt",
        "statement",
        "reconciliation_batch",
        "reconciliation_review",
        "parser_output",
        "calculation_case",
    }
)

VALID_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "calculated",
        "confirmed",
        "finalized",
        "failed",
        "superseded",
        "pending_confirmation",
    }
)


# -- domain model --


@dataclass(frozen=True)
class CalculationRunRecord:
    """Immutable record of a single deterministic calculation run.

    Mirrors the ``calc_audit_runs`` table schema.  Validation is
    performed on construction so invalid records are never created.
    """

    run_id: str
    run_type: str
    entity_type: str
    entity_id: str
    rule_version: str
    status: str
    source_type: str | None = None
    source_reference: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id is required")
        if not self.run_type:
            raise ValueError("run_type is required")
        if self.run_type not in VALID_RUN_TYPES:
            raise ValueError(
                f"Invalid run_type {self.run_type!r}; must be one of {sorted(VALID_RUN_TYPES)}"
            )
        if not self.entity_type:
            raise ValueError("entity_type is required")
        if self.entity_type not in VALID_ENTITY_TYPES:
            raise ValueError(
                f"Invalid entity_type {self.entity_type!r}; "
                f"must be one of {sorted(VALID_ENTITY_TYPES)}"
            )
        if not self.entity_id:
            raise ValueError("entity_id is required")
        if not self.rule_version:
            raise ValueError("rule_version is required")
        if not self.status:
            raise ValueError("status is required")
        if self.status not in VALID_STATUSES:
            raise ValueError(
                f"Invalid status {self.status!r}; must be one of {sorted(VALID_STATUSES)}"
            )
        if not self.created_at:
            raise ValueError("created_at is required")


# -- repository --


class CalculationRunRepository:
    """Repository for ``calc_audit_runs`` persistence.

    Accepts a caller-supplied ``sqlite3.Connection`` only.  Does not
    open database files, create default paths, or perform calculations.
    The caller is responsible for connection lifecycle, migration, and
    commit/rollback decisions.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(self, record: CalculationRunRecord) -> None:
        """Insert a new calculation run record.

        Raises ``sqlite3.IntegrityError`` if a run with the same
        ``run_id`` already exists (UNIQUE PRIMARY KEY constraint).
        """
        self._conn.execute(
            """\
            INSERT INTO calc_audit_runs (
                run_id,
                run_type,
                entity_type,
                entity_id,
                rule_version,
                status,
                source_type,
                source_reference,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.run_id,
                record.run_type,
                record.entity_type,
                record.entity_id,
                record.rule_version,
                record.status,
                record.source_type,
                record.source_reference,
                record.created_at,
            ),
        )

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    def fetch_by_run_id(self, run_id: str) -> CalculationRunRecord | None:
        """Return the record for *run_id*, or ``None`` if not found."""
        row = self._conn.execute(
            "SELECT * FROM calc_audit_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    def list_by_entity(
        self,
        entity_type: str,
        entity_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[CalculationRunRecord]:
        """Return runs for an entity, newest first."""
        rows = self._conn.execute(
            """\
            SELECT * FROM calc_audit_runs
            WHERE entity_type = ? AND entity_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (entity_type, entity_id, limit),
        ).fetchall()
        return tuple(_row_to_record(r) for r in rows)


# -- helpers --


def _row_to_record(row: Any) -> CalculationRunRecord:
    if not isinstance(row, sqlite3.Row):
        _columns = [
            "run_id",
            "run_type",
            "entity_type",
            "entity_id",
            "rule_version",
            "status",
            "source_type",
            "source_reference",
            "created_at",
        ]
        row = dict(zip(_columns, row))
    else:
        row = dict(row)

    return CalculationRunRecord(
        run_id=row["run_id"],
        run_type=row["run_type"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        rule_version=row["rule_version"],
        status=row["status"],
        source_type=row.get("source_type"),
        source_reference=row.get("source_reference"),
        created_at=row["created_at"],
    )


# -- public factory --


def make_run_record(
    *,
    run_id: str,
    run_type: str,
    entity_type: str,
    entity_id: str,
    rule_version: str,
    status: str = "pending",
    source_type: str | None = None,
    source_reference: str | None = None,
    created_at: str | None = None,
) -> CalculationRunRecord:
    """Create a validated ``CalculationRunRecord`` with an optional
    ``created_at`` timestamp.  When ``created_at`` is ``None``, the
    current UTC time is used.
    """
    ts = created_at or datetime.now(timezone.utc).isoformat()
    return CalculationRunRecord(
        run_id=run_id,
        run_type=run_type,
        entity_type=entity_type,
        entity_id=entity_id,
        rule_version=rule_version,
        status=status,
        source_type=source_type,
        source_reference=source_reference,
        created_at=ts,
    )


__all__ = [
    "CalculationRunRecord",
    "CalculationRunRepository",
    "make_run_record",
    "VALID_ENTITY_TYPES",
    "VALID_RUN_TYPES",
    "VALID_STATUSES",
]
