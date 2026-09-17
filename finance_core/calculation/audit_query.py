"""Calculation Audit Query Layer v1 -- read-only audit trail reconstruction.

Provides ``CalculationAuditTrail`` (immutable dataclass bundling a run
and its snapshots grouped by type) and ``CalculationAuditQueryService``
(caller-supplied ``sqlite3.Connection`` only, read-only).
This module never opens database files, performs calculations, or mutates records.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Sequence

from finance_core.calculation.run_persistence import (
    CalculationRunRecord,
    CalculationRunRepository,
)
from finance_core.calculation.snapshot_persistence import (
    CalculationSnapshotRecord,
    CalculationSnapshotRepository,
)

# -- domain model --


@dataclass(frozen=True)
class CalculationAuditTrail:
    """Immutable bundle of a calculation run and its associated snapshots.

    Snapshots are grouped by ``snapshot_type`` for convenient access. Unknown
    snapshot types are preserved in ``unknown_snapshots`` so future additive
    types are visible instead of being silently dropped by the query layer. A
    run with no snapshots is valid -- all snapshot fields will be empty.
    """

    run: CalculationRunRecord
    input_facts: Sequence[CalculationSnapshotRecord] = field(default_factory=tuple)
    rules: Sequence[CalculationSnapshotRecord] = field(default_factory=tuple)
    intermediate_values: Sequence[CalculationSnapshotRecord] = field(default_factory=tuple)
    output_result: Sequence[CalculationSnapshotRecord] = field(default_factory=tuple)
    unknown_snapshots: Sequence[CalculationSnapshotRecord] = field(default_factory=tuple)

    @property
    def all_snapshots(self) -> Sequence[CalculationSnapshotRecord]:
        """Return all snapshots across all types, ordered by type precedence."""
        result: list[CalculationSnapshotRecord] = []
        result.extend(self.input_facts)
        result.extend(self.rules)
        result.extend(self.intermediate_values)
        result.extend(self.output_result)
        result.extend(self.unknown_snapshots)
        return tuple(result)

    @property
    def snapshot_count(self) -> int:
        """Total number of snapshots in this audit trail."""
        return len(self.all_snapshots)

    @property
    def has_snapshots(self) -> bool:
        """Whether this audit trail has any snapshots."""
        return self.snapshot_count > 0


# -- query service --


class CalculationAuditQueryService:
    """Read-only query service for the calculation audit architecture.

    Composes ``CalculationRunRepository`` and ``CalculationSnapshotRepository``
    to reconstruct a full audit trail from ``calc_audit_runs`` and
    ``calculation_snapshots``.

    Accepts a caller-supplied ``sqlite3.Connection`` only.  Does not
    open database files, create default paths, perform calculations,
    or mutate records.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._run_repo = CalculationRunRepository(conn)
        self._snap_repo = CalculationSnapshotRepository(conn)

    # ------------------------------------------------------------------
    # get_run
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> CalculationRunRecord | None:
        """Return the calculation run record for *run_id*, or ``None`` if
        not found."""
        return self._run_repo.fetch_by_run_id(run_id)

    # ------------------------------------------------------------------
    # get_snapshots
    # ------------------------------------------------------------------

    def get_snapshots(self, run_id: str) -> Sequence[CalculationSnapshotRecord]:
        """Return all snapshots for *run_id*, ordered by ``created_at``
        ascending.  Returns an empty sequence if the run has no snapshots
        or does not exist."""
        return self._snap_repo.list_by_run_id(run_id)

    # ------------------------------------------------------------------
    # get_audit_trail
    # ------------------------------------------------------------------

    def get_audit_trail(self, run_id: str) -> CalculationAuditTrail | None:
        """Reconstruct the full audit trail for *run_id*.

        Returns ``None`` if no run exists with *run_id*.  If the run
        exists but has no snapshots, returns a ``CalculationAuditTrail``
        with the run and empty snapshot groups.
        """
        run = self._run_repo.fetch_by_run_id(run_id)
        if run is None:
            return None

        snapshots = self._snap_repo.list_by_run_id(run_id)

        # Group snapshots by type
        input_facts: list[CalculationSnapshotRecord] = []
        rules: list[CalculationSnapshotRecord] = []
        intermediate_values: list[CalculationSnapshotRecord] = []
        output_result: list[CalculationSnapshotRecord] = []
        unknown_snapshots: list[CalculationSnapshotRecord] = []

        for snap in snapshots:
            stype = snap.snapshot_type
            if stype == "input_facts":
                input_facts.append(snap)
            elif stype == "rules":
                rules.append(snap)
            elif stype == "intermediate_values":
                intermediate_values.append(snap)
            elif stype == "output_result":
                output_result.append(snap)
            else:
                unknown_snapshots.append(snap)

        return CalculationAuditTrail(
            run=run,
            input_facts=tuple(input_facts),
            rules=tuple(rules),
            intermediate_values=tuple(intermediate_values),
            output_result=tuple(output_result),
            unknown_snapshots=tuple(unknown_snapshots),
        )

    # ------------------------------------------------------------------
    # list_runs_for_entity
    # ------------------------------------------------------------------

    def list_runs_for_entity(
        self,
        entity_type: str,
        entity_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[CalculationRunRecord]:
        """Return all calculation runs for an entity, newest first."""
        return self._run_repo.list_by_entity(entity_type, entity_id, limit=limit)


__all__ = [
    "CalculationAuditTrail",
    "CalculationAuditQueryService",
]
