"""Reconciliation Apply Persistence v1 -- persists resolution apply results
into the reconciliation_apply_results table.

All methods operate on an externally-provided ``sqlite3.Connection``.
The caller is responsible for connection lifecycle and ensuring the
reconciliation persistence schema (including migration 008) is already
applied.

Key design properties:
- Never opens or modifies database/finance.db.
- Deterministic JSON serialization for payload, audit_evidence, and references.
- Idempotent: saving the same apply_id or same decision_id with identical
  fingerprint is safe.
- Conflict detection: same apply_id or same decision_id with different
  fingerprint raises an error.
- Each decision_id can have at most one persisted apply result (UNIQUE
  constraint on decision_id).
- Does not silently overwrite previously persisted different results.
- Does NOT create, update, delete, merge, or overwrite final financial
  transaction records.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence

from finance_core.reconciliation.models import ResolutionApplyResult


class ApplyPersistenceConflictError(Exception):
    """Raised when a persisted apply result conflicts with an existing row."""


# ---------------------------------------------------------------------------
# Persistence adapter
# ---------------------------------------------------------------------------


class ApplyPersistence:
    """SQLite persistence adapter for ResolutionApplyResult objects.

    Usage::

        conn = connect_sqlite("/tmp/recon_apply_demo.db")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # ... apply migrations 001-008 ...
        ap = ApplyPersistence(conn)
        ap.save_apply_result(result)
        rows = ap.list_apply_results_for_queue_item("q-001")
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_apply_result(self, result: ResolutionApplyResult) -> bool:
        """Insert a single apply result row.

        Returns True when a new row is inserted, False when an identical row
        already exists (idempotent replay).

        Raises ``ApplyPersistenceConflictError`` when the same ``apply_id``
        exists with a different fingerprint or payload.
        """
        inserted = self._insert_result(result)
        self._conn.commit()
        return inserted

    def persist_apply_results(self, results: Sequence[ResolutionApplyResult]) -> int:
        """Persist a batch of apply results.

        Returns the number of newly inserted rows.
        """
        count = 0
        for result in results:
            if self._insert_result(result):
                count += 1
        self._conn.commit()
        return count

    def _insert_result(self, result: ResolutionApplyResult) -> bool:
        fingerprint = _build_fingerprint(result)
        values = (
            result.apply_id,
            result.decision_id,
            result.queue_item_id,
            result.candidate_id or None,
            result.action.value,
            1 if result.success else 0,
            1 if result.idempotent else 0,
            _serialize_payload(result.payload),
            _serialize_audit_evidence(result.audit_evidence),
            _serialize_ref(result.statement_reference),
            _serialize_ref(result.app_transaction_reference),
            result.reviewer or None,
            result.note or None,
            fingerprint,
            result.applied_at,
        )

        try:
            self._conn.execute(
                """
                INSERT INTO reconciliation_apply_results (
                    apply_id, decision_id, queue_item_id, candidate_id,
                    action, success, idempotent,
                    payload_json, audit_evidence_json,
                    statement_reference_json, app_transaction_reference_json,
                    reviewer, note, fingerprint, applied_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            return True
        except sqlite3.IntegrityError as exc:
            if not (_is_unique_apply_id_error(exc) or _is_unique_decision_id_error(exc)):
                raise

            # Check apply_id first (primary uniqueness contract), then decision_id
            existing = self._get_by_apply_id(result.apply_id)
            if existing is None:
                existing = self._get_by_decision_id(result.decision_id)
                if existing is None:
                    raise ApplyPersistenceConflictError(
                        f"Unexpected: unique constraint violated for apply_id "
                        f"'{result.apply_id}' / decision_id '{result.decision_id}' "
                        f"but row not found on read-back."
                    ) from exc
                conflict_id = f"decision_id '{result.decision_id}'"
            else:
                conflict_id = f"apply_id '{result.apply_id}'"

            if existing["fingerprint"] == fingerprint:
                return False  # Idempotent

            raise ApplyPersistenceConflictError(
                f"Conflicting {conflict_id}: "
                f"existing fingerprint '{existing['fingerprint'][:16]}...' "
                f"does not match new fingerprint '{fingerprint[:16]}...'"
            ) from exc

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_apply_result_by_apply_id(self, apply_id: str) -> sqlite3.Row | None:
        """Return the apply result row for the given apply_id, or None."""
        return self._get_by_apply_id(apply_id)

    def get_apply_result_by_decision_id(self, decision_id: str) -> sqlite3.Row | None:
        """Return the apply result row for the given decision_id, or None."""
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_apply_results
            WHERE decision_id = ?
            """,
            (decision_id,),
        ).fetchone()

    def list_apply_results_for_queue_item(self, queue_item_id: str) -> list[sqlite3.Row]:
        """Return all apply results for a given queue item, ordered by
        applied_at ascending."""
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_apply_results
            WHERE queue_item_id = ?
            ORDER BY applied_at ASC
            """,
            (queue_item_id,),
        ).fetchall()

    def has_apply_result_for_queue_item(self, queue_item_id: str) -> bool:
        """Return True if at least one apply result exists for the queue item."""
        row = self._conn.execute(
            """
            SELECT 1 FROM reconciliation_apply_results
            WHERE queue_item_id = ?
            LIMIT 1
            """,
            (queue_item_id,),
        ).fetchone()
        return row is not None

    def list_all(self) -> list[sqlite3.Row]:
        """Return all apply result rows, ordered by applied_at descending."""
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_apply_results
            ORDER BY applied_at DESC
            """
        ).fetchall()

    def _get_by_apply_id(self, apply_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_apply_results
            WHERE apply_id = ?
            """,
            (apply_id,),
        ).fetchone()

    def _get_by_decision_id(self, decision_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_apply_results
            WHERE decision_id = ?
            """,
            (decision_id,),
        ).fetchone()


# ---------------------------------------------------------------------------
# Serialization helpers (deterministic)
# ---------------------------------------------------------------------------

_EMPTY_DICT: dict[str, Any] = {}
_EMPTY_LIST: list[Any] = []


def _serialize_payload(payload: dict[str, Any]) -> str:
    if not payload:
        return json.dumps(_EMPTY_DICT, sort_keys=True, separators=(",", ":"))
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _serialize_audit_evidence(evidence: dict[str, Any]) -> str:
    if not evidence:
        return json.dumps(_EMPTY_DICT, sort_keys=True, separators=(",", ":"))
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str)


def _serialize_ref(ref: str | None) -> str | None:
    """Serialize a single reference value to JSON null or a quoted string."""
    if ref is None:
        return None
    return json.dumps(ref, sort_keys=True, separators=(",", ":"))


def _build_fingerprint(result: ResolutionApplyResult) -> str:
    """Build a stable fingerprint from the apply result.

    Used for idempotency checks: same apply_id with different fingerprint
    is a conflict. Uses SHA-256 for collision resistance.
    """
    import hashlib

    canonical = {
        "apply_id": result.apply_id,
        "decision_id": result.decision_id,
        "queue_item_id": result.queue_item_id,
        "action": result.action.value,
        "success": result.success,
        "payload": result.payload,
        "reviewer": result.reviewer,
        "note": result.note,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_unique_apply_id_error(exc: sqlite3.IntegrityError) -> bool:
    message = str(exc).lower()
    return "unique" in message and "apply_id" in message


def _is_unique_decision_id_error(exc: sqlite3.IntegrityError) -> bool:
    message = str(exc).lower()
    return "unique" in message and "decision_id" in message


__all__ = [
    "ApplyPersistence",
    "ApplyPersistenceConflictError",
]
