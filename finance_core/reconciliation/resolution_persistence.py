"""Reconciliation Resolution Persistence v1 -- persists resolution
decisions and results into reconciliation_resolution_decisions and
reconciliation_resolution_results tables.

All methods operate on an externally-provided ``sqlite3.Connection``.
The caller is responsible for connection lifecycle and ensuring the
reconciliation persistence schema (including migration 007) is already
applied.

Key design properties:
- Never opens or modifies database/finance.db.
- Applies resolution decisions through the ResolutionRuntime.
- Updates review queue status based on resolution outcome.
- Records audit evidence deterministically.
- Does not mutate real app transactions or statement transactions.
"""

from __future__ import annotations

import json
import sqlite3

from finance_core.reconciliation.models import (
    ResolutionAction,
    ResolutionDecision,
    ResolutionResult,
    ReviewQueueItem,
)
from finance_core.reconciliation.resolution import ResolutionRuntime


class DuplicateResolutionPersistenceError(Exception):
    """Raised when an existing resolution audit row conflicts with new data."""


# ---------------------------------------------------------------------------
# Status transition rules (deterministic)
# ---------------------------------------------------------------------------

_SUCCESSFUL_STATUS_MAP: dict[ResolutionAction, str] = {
    ResolutionAction.CONFIRM_MATCH: "resolved",
    ResolutionAction.ADJUST_APP_TRANSACTION: "resolved",
    ResolutionAction.CREATE_MISSING_APP_TRANSACTION: "resolved",
    ResolutionAction.MARK_STATEMENT_ONLY: "resolved",
    ResolutionAction.MARK_DUPLICATE: "resolved",
    ResolutionAction.IGNORE: "ignored",
    ResolutionAction.NEEDS_MORE_INFO: "needs_more_info",
}
"""Mapping from resolution action to review queue status when the resolution
   result is successful."""


class ResolutionPersistence:
    """Persists ResolutionDecision and ResolutionResult objects and
    applies resolution outcomes to review queue status.

    Usage::

        conn = connect_sqlite("/tmp/review_demo.db")
        rp = ResolutionPersistence(conn)
        rp.persist_decision(decision)
        result = runtime.resolve(item, decision)
        rp.persist_result(result)
        if result.success:
            rp.apply_resolution(item, decision, result)
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._runtime = ResolutionRuntime()

    # ------------------------------------------------------------------
    # Write decisions
    # ------------------------------------------------------------------

    def persist_decision(self, decision: ResolutionDecision) -> bool:
        """Insert a resolution decision row.

        Idempotent only when the duplicate public_id carries identical data.
        Returns True when a row is inserted and False when an identical row
        already exists.
        """
        inserted = self._insert_decision(decision)
        self._conn.commit()
        return inserted

    def _insert_decision(self, decision: ResolutionDecision) -> bool:
        try:
            self._conn.execute(
                """
                INSERT INTO reconciliation_resolution_decisions (
                    public_id, review_queue_public_id, decision_action,
                    decision_note, reviewer, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                _decision_values(decision),
            )
            return True
        except sqlite3.IntegrityError as exc:
            if not _is_unique_public_id_error(exc):
                raise
            existing = self._get_decision_by_public_id(decision.decision_id)
            if existing is not None and _decision_row_matches(existing, decision):
                return False
            raise DuplicateResolutionPersistenceError(
                f"Conflicting resolution decision public_id: {decision.decision_id}"
            ) from exc

    # ------------------------------------------------------------------
    # Write results
    # ------------------------------------------------------------------

    def persist_result(self, result: ResolutionResult) -> bool:
        """Insert a resolution result row.

        Serializes audit_evidence to JSON deterministically. Idempotent only
        when the duplicate public_id carries identical data.
        """
        inserted = self._insert_result(result)
        self._conn.commit()
        return inserted

    def _insert_result(self, result: ResolutionResult) -> bool:
        try:
            self._conn.execute(
                """
                INSERT INTO reconciliation_resolution_results (
                    public_id, review_queue_public_id, decision_public_id,
                    success, error_message, audit_evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                _result_values(result),
            )
            return True
        except sqlite3.IntegrityError as exc:
            if not _is_unique_public_id_error(exc):
                raise
            existing = self._get_result_by_public_id(result.result_id)
            if existing is not None and _result_row_matches(existing, result):
                return False
            raise DuplicateResolutionPersistenceError(
                f"Conflicting resolution result public_id: {result.result_id}"
            ) from exc

    # ------------------------------------------------------------------
    # Apply resolution
    # ------------------------------------------------------------------

    def apply_resolution(
        self,
        item: ReviewQueueItem,
        decision: ResolutionDecision,
        result: ResolutionResult | None = None,
    ) -> ResolutionResult:
        """Apply a resolution decision to a review queue item.

        1. Runs the decision through ResolutionRuntime if result is not
           already provided.
        2. Persists the decision.
        3. Persists the result.
        4. Updates the review queue status based on outcome.

        Returns the ResolutionResult.
        """
        if result is None:
            result = self._runtime.resolve(item, decision)

        try:
            self._insert_decision(decision)
            self._insert_result(result)

            if result.success:
                new_status = _SUCCESSFUL_STATUS_MAP.get(decision.action, "pending")
            else:
                new_status = "pending"

            self._conn.execute(
                """
                UPDATE reconciliation_review_queue
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE public_id = ?
                """,
                (new_status, item.queue_item_id),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        return result

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_decisions_for_queue_item(self, queue_public_id: str) -> list[sqlite3.Row]:
        """Return all decisions for a given review queue item."""
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_resolution_decisions
            WHERE review_queue_public_id = ?
            ORDER BY created_at ASC
            """,
            (queue_public_id,),
        ).fetchall()

    def _get_decision_by_public_id(self, public_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_resolution_decisions
            WHERE public_id = ?
            """,
            (public_id,),
        ).fetchone()

    def list_results_for_queue_item(self, queue_public_id: str) -> list[sqlite3.Row]:
        """Return all results for a given review queue item."""
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_resolution_results
            WHERE review_queue_public_id = ?
            ORDER BY created_at ASC
            """,
            (queue_public_id,),
        ).fetchall()

    def _get_result_by_public_id(self, public_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_resolution_results
            WHERE public_id = ?
            """,
            (public_id,),
        ).fetchone()


def _decision_values(
    decision: ResolutionDecision,
) -> tuple[str, str, str, str | None, str, str | None]:
    return (
        decision.decision_id,
        decision.queue_item_id,
        decision.action.value,
        decision.note or None,
        decision.reviewer,
        decision.resolved_at or None,
    )


def _result_values(result: ResolutionResult) -> tuple[str, str, str, int, str | None, str]:
    audit_json = json.dumps(
        result.audit_evidence,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        result.result_id,
        result.queue_item.queue_item_id,
        result.decision.decision_id,
        1 if result.success else 0,
        result.error_message or None,
        audit_json,
    )


def _is_unique_public_id_error(exc: sqlite3.IntegrityError) -> bool:
    message = str(exc).lower()
    return "unique" in message and "public_id" in message


def _decision_row_matches(row: sqlite3.Row, decision: ResolutionDecision) -> bool:
    return (
        row["review_queue_public_id"] == decision.queue_item_id
        and row["decision_action"] == decision.action.value
        and row["decision_note"] == (decision.note or None)
        and row["reviewer"] == decision.reviewer
        and row["resolved_at"] == (decision.resolved_at or None)
    )


def _result_row_matches(row: sqlite3.Row, result: ResolutionResult) -> bool:
    values = _result_values(result)
    return (
        row["review_queue_public_id"] == values[1]
        and row["decision_public_id"] == values[2]
        and row["success"] == values[3]
        and row["error_message"] == values[4]
        and row["audit_evidence_json"] == values[5]
    )


__all__ = [
    "DuplicateResolutionPersistenceError",
    "ResolutionPersistence",
    "_SUCCESSFUL_STATUS_MAP",
]
