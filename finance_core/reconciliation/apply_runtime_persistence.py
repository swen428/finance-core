"""SQLite-backed persistence for guarded reconciliation apply runtime
execution results and idempotency state.

This adapter persists ``GuardedApplyExecutionResult`` objects and their
``GuardedOperationResult`` children into SQLite for durable idempotency
and audit evidence across process restarts. It never writes final
financial records.

Key invariants:
- No final financial record mutation.
- No settlement obligation generation.
- No Telegram / OCR / PDF / Metabase interaction.
- Accepts an explicit ``sqlite3.Connection``; never opens
  ``database/finance.db`` itself.
- Deterministic JSON serialization via ``sort_keys=True`` and
  ``separators=(",", ":")``.
- idempotency_key is UNIQUE: same key + same fingerprint returns cached
  result; same key + different fingerprint returns CONFLICT.
- Every save is atomic: execution row + operation result rows are
  inserted in one transaction.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from finance_core.reconciliation.models import (
    ApplyExecutionStatus,
    GuardedApplyExecutionResult,
    GuardedOperationResult,
)

# ---------------------------------------------------------------------------
# Repository -- GuardedApplyExecutionRepository
# ---------------------------------------------------------------------------


class GuardedApplyExecutionRepository:
    """SQLite persistence adapter for guarded apply execution results.

    Usage::

        conn = connect_sqlite(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # ... apply migrations ...
        repo = GuardedApplyExecutionRepository(conn)
        repo.save_execution_result(
            result=...,
            execution_fingerprint="abc123",
        )
        cached = repo.get_by_idempotency_key("key-001")

    .. note::

       The constructor sets ``conn.row_factory = sqlite3.Row``. The
       repository relies on row-based column access for all internal
       read paths. Callers that share the same ``sqlite3.Connection``
       with other components should be aware that this is a
       connection-level side effect that affects all subsequent
       cursor rows on that connection.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_execution_result(
        self,
        result: GuardedApplyExecutionResult,
        *,
        execution_fingerprint: str,
    ) -> bool:
        """Persist a guarded apply execution result and its operation results.

        Returns True when a new execution row is inserted. Returns False
        when an identical execution already exists (idempotent replay:
        same idempotency_key and same fingerprint).

        Raises ``GuardedApplyExecutionConflictError`` when the same
        ``idempotency_key`` exists with a different fingerprint.
        """
        # --- Check for existing key first (outside transaction) ---
        existing = self._conn.execute(
            """
            SELECT idempotency_key, execution_fingerprint, execution_status
            FROM reconciliation_guarded_apply_executions
            WHERE idempotency_key = ?
            """,
            (result.idempotency_key,),
        ).fetchone()

        if existing is not None:
            if existing["execution_fingerprint"] == execution_fingerprint:
                return False  # Idempotent replay
            raise GuardedApplyExecutionConflictError(
                f"Idempotency key '{result.idempotency_key}' already exists with "
                f"a different execution fingerprint. "
                f"Existing fingerprint: {existing['execution_fingerprint'][:16]}..., "
                f"status: {existing['execution_status']}."
            )

        # --- Insert execution + operations atomically ---
        try:
            self._conn.execute("SAVEPOINT save_ga_exec")
            self._insert_execution(result, execution_fingerprint)
            self._insert_operation_results(result)
            self._conn.execute("RELEASE save_ga_exec")
            self._conn.commit()
            return True
        except Exception:
            self._conn.execute("ROLLBACK TO save_ga_exec")
            self._conn.commit()
            raise

    def _insert_execution(
        self,
        result: GuardedApplyExecutionResult,
        fingerprint: str,
    ) -> None:
        execution_id = _derive_execution_id(result.plan_id, result.idempotency_key)
        self._conn.execute(
            """
            INSERT INTO reconciliation_guarded_apply_executions (
                execution_id, plan_id, idempotency_key,
                execution_fingerprint, execution_status,
                total_operations, operations_executed,
                operations_blocked, operations_skipped,
                block_reason, guard_decision_refs_json,
                audit_trail_json, is_dry_run, executed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                result.plan_id,
                result.idempotency_key,
                fingerprint,
                result.execution_status.value,
                result.total_operations,
                result.operated_executed,
                result.operated_blocked,
                result.operated_skipped,
                result.block_reason,
                _serialize_json(tuple(result.guard_decision_refs)),
                _serialize_json(result.audit_trail),
                1 if result.is_dry_run else 0,
                result.executed_at,
            ),
        )

    def _insert_operation_results(self, result: GuardedApplyExecutionResult) -> None:
        execution_id = _derive_execution_id(result.plan_id, result.idempotency_key)
        for op_result in result.results:
            operation_result_id = f"{execution_id}-{op_result.operation_id}"
            self._conn.execute(
                """
                INSERT INTO reconciliation_guarded_apply_operation_results (
                    operation_result_id, execution_id, operation_id,
                    decision_id, execution_status, reason,
                    guard_decision_approved,
                    guard_decision_idempotency_key,
                    guard_blocked_reasons_json,
                    mutation_type, mutation_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_result_id,
                    execution_id,
                    op_result.operation_id,
                    op_result.decision_id,
                    op_result.execution_status.value,
                    op_result.reason,
                    1 if op_result.guard_decision_approved else 0,
                    op_result.guard_decision_idempotency_key,
                    _serialize_json(tuple(op_result.guard_blocked_reasons)),
                    op_result.mutation_type,
                    _serialize_json(op_result.mutation_payload),
                ),
            )

    # ------------------------------------------------------------------
    # Read -- idempotency key
    # ------------------------------------------------------------------

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[GuardedApplyExecutionResult]:
        """Load a cached execution result by idempotency key.

        Returns ``None`` when no execution exists for this key.
        """
        row = self._conn.execute(
            """
            SELECT * FROM reconciliation_guarded_apply_executions
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_result(row)

    # ------------------------------------------------------------------
    # Read -- execution_id
    # ------------------------------------------------------------------

    def get_by_execution_id(self, execution_id: str) -> Optional[GuardedApplyExecutionResult]:
        """Load an execution result by execution_id."""
        row = self._conn.execute(
            """
            SELECT * FROM reconciliation_guarded_apply_executions
            WHERE execution_id = ?
            """,
            (execution_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_result(row)

    def list_execution_results(
        self,
        *,
        execution_status: ApplyExecutionStatus | None = None,
    ) -> list[GuardedApplyExecutionResult]:
        """List persisted execution results in deterministic timeline order.

        This is a read-only operator/reviewer query. It derives domain
        objects from persisted execution rows and never updates execution
        state or final financial records.
        """
        if execution_status is None:
            rows = self._conn.execute(
                """
                SELECT * FROM reconciliation_guarded_apply_executions
                ORDER BY executed_at DESC, idempotency_key ASC
                """
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM reconciliation_guarded_apply_executions
                WHERE execution_status = ?
                ORDER BY executed_at DESC, idempotency_key ASC
                """,
                (execution_status.value,),
            ).fetchall()
        return [self._row_to_result(row) for row in rows]

    # ------------------------------------------------------------------
    # Read -- idempotency check (lightweight)
    # ------------------------------------------------------------------

    def has_idempotency_key(self, idempotency_key: str) -> bool:
        """Return True when an execution exists for this idempotency key."""
        row = self._conn.execute(
            """
            SELECT 1 FROM reconciliation_guarded_apply_executions
            WHERE idempotency_key = ?
            LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()
        return row is not None

    def get_idempotency_fingerprint(self, idempotency_key: str) -> Optional[str]:
        """Return the stored fingerprint for an idempotency key, or None."""
        row = self._conn.execute(
            """
            SELECT execution_fingerprint
            FROM reconciliation_guarded_apply_executions
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return row["execution_fingerprint"]

    # ------------------------------------------------------------------
    # Read -- operation results
    # ------------------------------------------------------------------

    def list_operation_results(self, execution_id: str) -> list[GuardedOperationResult]:
        """Return all operation results for a given execution_id."""
        rows = self._conn.execute(
            """
            SELECT * FROM reconciliation_guarded_apply_operation_results
            WHERE execution_id = ?
            ORDER BY operation_id ASC
            """,
            (execution_id,),
        ).fetchall()
        return [self._op_row_to_result(r) for r in rows]

    # ------------------------------------------------------------------
    # Build domain objects from SQLite rows
    # ------------------------------------------------------------------

    def _row_to_result(self, row: sqlite3.Row) -> GuardedApplyExecutionResult:
        execution_id = row["execution_id"]
        op_rows = self._conn.execute(
            """
            SELECT * FROM reconciliation_guarded_apply_operation_results
            WHERE execution_id = ?
            ORDER BY operation_id ASC
            """,
            (execution_id,),
        ).fetchall()

        results = tuple(self._op_row_to_result(r) for r in op_rows)

        guard_refs = _deserialize_tuple(row["guard_decision_refs_json"])
        audit_trail = _deserialize_dict(row["audit_trail_json"])

        return GuardedApplyExecutionResult(
            plan_id=row["plan_id"],
            idempotency_key=row["idempotency_key"],
            execution_status=ApplyExecutionStatus(row["execution_status"]),
            results=results,
            total_operations=row["total_operations"],
            operated_executed=row["operations_executed"],
            operated_blocked=row["operations_blocked"],
            operated_skipped=row["operations_skipped"],
            block_reason=row["block_reason"],
            guard_decision_refs=guard_refs,
            executed_at=row["executed_at"],
            audit_trail=audit_trail,
            is_dry_run=bool(row["is_dry_run"]),
        )

    @staticmethod
    def _op_row_to_result(row: sqlite3.Row) -> GuardedOperationResult:
        return GuardedOperationResult(
            operation_id=row["operation_id"],
            decision_id=row["decision_id"],
            execution_status=ApplyExecutionStatus(row["execution_status"]),
            reason=row["reason"],
            guard_decision_approved=bool(row["guard_decision_approved"]),
            guard_decision_idempotency_key=row["guard_decision_idempotency_key"],
            guard_blocked_reasons=_deserialize_tuple(row["guard_blocked_reasons_json"]),
            mutation_type=row["mutation_type"],
            mutation_payload=_deserialize_dict(row["mutation_payload_json"]),
        )


# ---------------------------------------------------------------------------
# Conflict error
# ---------------------------------------------------------------------------


class GuardedApplyExecutionConflictError(Exception):
    """Raised when saving an execution result conflicts with an existing
    persisted row (different fingerprint for the same idempotency key)."""


# ---------------------------------------------------------------------------
# Serialization helpers (deterministic)
# ---------------------------------------------------------------------------


def _serialize_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _deserialize_tuple(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    loaded = json.loads(raw)
    if isinstance(loaded, list):
        return tuple(loaded)
    return ()


def _deserialize_dict(raw: str | None) -> dict:
    if not raw:
        return {}
    loaded = json.loads(raw)
    if isinstance(loaded, dict):
        return loaded
    return {}


# ---------------------------------------------------------------------------
# Execution ID derivation (deterministic)
# ---------------------------------------------------------------------------


def _derive_execution_id(plan_id: str, idempotency_key: str) -> str:
    """Derive a stable execution_id from plan_id and idempotency_key.

    Uses SHA-256 to produce a deterministic, collision-resistant ID
    without introducing timestamps or randomness.
    """
    import hashlib

    raw = f"{plan_id}|{idempotency_key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"exec-{digest[:16]}"


__all__ = [
    "GuardedApplyExecutionRepository",
    "GuardedApplyExecutionConflictError",
]
