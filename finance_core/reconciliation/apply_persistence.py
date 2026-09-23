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
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Sequence

from finance_core.application.correction_schema import (
    has_committed_correction,
    verify_correction_schema,
)
from finance_core.reconciliation.models import (
    ResolutionApplyResult,
    validate_resolution_decision,
)
from finance_core.reconciliation.resolution_integrity import (
    assert_apply_basic,
    assert_apply_envelope,
)
from finance_core.reconciliation.source_binding import load_bound_queue


class ApplyPersistenceConflictError(Exception):
    """Raised when a persisted apply result conflicts with an existing row."""


def _reject_source_identity(conn: sqlite3.Connection, result: ResolutionApplyResult) -> None:
    if conn.execute(
        "SELECT 1 FROM reconciliation_apply_results WHERE apply_id = ?", (result.apply_id,)
    ).fetchone():
        raise ApplyPersistenceConflictError(f"Conflicting apply_id '{result.apply_id}'")
    existing_decision = conn.execute(
        "SELECT 1 FROM reconciliation_apply_results WHERE decision_id = ?",
        (result.decision_id,),
    ).fetchone()
    if existing_decision is not None:
        raise ApplyPersistenceConflictError(f"Conflicting decision_id '{result.decision_id}'")
    raise ValueError("successful reconciliation apply source identity changed")


@contextmanager
def _owned_write(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        raise RuntimeError("reconciliation apply requires a connection without caller-owned work")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _guard_successful_apply(conn: sqlite3.Connection, result: ResolutionApplyResult) -> None:
    if type(result.success) is not bool or type(result.idempotent) is not bool:
        raise ValueError("reconciliation apply success and idempotent must be boolean")
    if result.success and verify_correction_schema(conn):
        try:
            assert_apply_basic(result)
        except ValueError:
            if conn.execute(
                "SELECT 1 FROM reconciliation_apply_results WHERE apply_id = ? OR decision_id = ?",
                (result.apply_id, result.decision_id),
            ).fetchone():
                _reject_source_identity(conn, result)
            raise
    if not result.success or result.action.value not in {"confirm_match", "mark_duplicate"}:
        return
    bound = load_bound_queue(conn, result.queue_item_id)
    if bound is not None:
        try:
            assert_apply_envelope(result, bound)
        except ValueError:
            if conn.execute(
                "SELECT 1 FROM reconciliation_apply_results WHERE apply_id = ? OR decision_id = ?",
                (result.apply_id, result.decision_id),
            ).fetchone():
                _reject_source_identity(conn, result)
            raise
    payload = result.payload
    targets: set[str] = set()
    if result.action.value == "confirm_match":
        target = payload.get("app_txn_id")
        if (
            not isinstance(target, str)
            or not target
            or payload.get("action_type") != "confirm_match"
            or result.app_transaction_reference != target
        ):
            raise ValueError("successful reconciliation has incomplete app target enumeration")
        targets.add(target)
    else:
        duplicates = payload.get("duplicate_app_txn_ids")
        kept = payload.get("kept_app_txn_id")
        if (
            not isinstance(duplicates, list)
            or len(duplicates) < 2
            or not all(isinstance(value, str) and value for value in duplicates)
            or len(set(duplicates)) != len(duplicates)
            or not isinstance(kept, str)
            or kept not in duplicates
            or payload.get("action_type") != "mark_duplicate"
            or payload.get("audit_only") is not True
            or (
                result.app_transaction_reference is not None
                and result.app_transaction_reference not in duplicates
            )
        ):
            raise ValueError("successful duplicate classification has incomplete app targets")
        targets.update(duplicates)
        targets.add(kept)
    if bound is not None:
        compatible, _ = validate_resolution_decision(result.action, bound.issue_type)
        if not compatible:
            raise ValueError(
                "successful reconciliation apply action conflicts with frozen queue issue"
            )
        if (
            result.candidate_id != bound.candidate_id
            or result.statement_reference != bound.statement_transaction_ref
            or result.app_transaction_reference != bound.app_transaction_ref
            or result.audit_evidence.get("queue_item_id") != bound.queue_item_id
            or result.audit_evidence.get("candidate_id") != bound.candidate_id
            or result.audit_evidence.get("resolution_action") != result.action.value
            or result.audit_evidence.get("issue_type") != bound.issue_type.value
            or result.audit_evidence.get("decision_id") != result.decision_id
            or result.audit_evidence.get("reviewer") != result.reviewer
            or (result.audit_evidence.get("note") or None) != (result.note or None)
        ):
            _reject_source_identity(conn, result)
        if result.action.value == "mark_duplicate":
            if (
                payload.get("duplicate_app_txn_ids")
                != [app.app_txn_id for app in bound.app_transactions]
                or payload.get("kept_app_txn_id") != bound.app_transaction_ref
            ):
                raise ValueError(
                    "successful duplicate apply targets differ from frozen queue source"
                )
        elif payload.get("app_txn_id") != bound.app_transaction_ref:
            raise ValueError(
                "successful reconciliation apply target differs from frozen queue source"
            )
        targets.update(app.app_txn_id for app in bound.app_transactions)
    if result.app_transaction_reference is not None:
        targets.add(result.app_transaction_reference)
    if any(has_committed_correction(conn, target) for target in sorted(targets)):
        raise ValueError("corrected_transaction_requires_versioned_reconciliation")


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
        with _owned_write(self._conn):
            _guard_successful_apply(self._conn, result)
            return self._insert_result(result)

    def persist_apply_results(self, results: Sequence[ResolutionApplyResult]) -> int:
        """Persist a batch of apply results.

        Returns the number of newly inserted rows.
        """
        with _owned_write(self._conn):
            count = 0
            for result in results:
                _guard_successful_apply(self._conn, result)
                if self._insert_result(result):
                    count += 1
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

        # 051's collision guard fires before SQLite reports a UNIQUE error.
        # BEGIN IMMEDIATE makes this lookup and insert one owned transaction.
        existing = self._get_by_apply_id(result.apply_id)
        if existing is None:
            existing = self._get_by_decision_id(result.decision_id)
            conflict_id = f"decision_id '{result.decision_id}'"
        else:
            conflict_id = f"apply_id '{result.apply_id}'"
        if existing is not None:
            # The historical fingerprint omits source refs, timestamps and
            # audit JSON.  Replay requires the complete stored claim; only
            # the runtime's replay flag may differ.
            columns = (
                "apply_id", "decision_id", "queue_item_id", "candidate_id",
                "action", "success", "idempotent", "payload_json",
                "audit_evidence_json", "statement_reference_json",
                "app_transaction_reference_json", "reviewer", "note",
                "fingerprint", "applied_at",
            )
            same_claim = all(
                existing[column] == value
                for column, value in zip(columns, values, strict=True)
                if column not in {"idempotent", "fingerprint"}
            )
            if existing["fingerprint"] == fingerprint and same_claim:
                return False
            raise ApplyPersistenceConflictError(
                f"Conflicting {conflict_id}: "
                f"existing fingerprint '{existing['fingerprint'][:16]}...' "
                f"does not match new fingerprint '{fingerprint[:16]}...'"
            )
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


__all__ = [
    "ApplyPersistence",
    "ApplyPersistenceConflictError",
]
