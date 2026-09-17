"""SQLite-backed reconciliation batch apply state control.

This adapter persists the ``BatchApplyStateManager`` lifecycle semantics into
the migration 012 tables so duplicate prevention survives process restarts.
It does not persist final financial facts and does not modify live data.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from finance_core.reconciliation.apply import apply_decisions
from finance_core.reconciliation.migrations import LIVE_DB_PATH
from finance_core.reconciliation.models import (
    ApplyConflictError,
    BatchApplyResult,
    BatchApplyState,
    ResolutionAction,
    ResolutionApplyResult,
    ResolutionDecision,
    ReviewQueueItem,
)


class SQLiteBatchApplyStateError(Exception):
    """Raised when persisted batch state cannot be read or written safely."""


class SQLiteBatchApplyStateManager:
    """SQLite-backed manager that enforces batch apply state control.

    The public methods intentionally mirror ``BatchApplyStateManager`` so
    callers can use the persistent adapter where cross-process duplicate
    prevention is required.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._guard_live_database()

    def apply_batch(
        self,
        batch_id: str,
        items: list[ReviewQueueItem],
        decisions: list[ResolutionDecision],
        *,
        evidence_refs_by_queue_item: Mapping[str, tuple[str, ...]] | None = None,
        require_evidence_ref: bool = False,
        idempotency_key: str | None = None,
    ) -> BatchApplyResult:
        """Apply a batch with persisted state and transition audit rows."""
        now_iso = _utc_now()
        previous_row = self._get_batch_row(batch_id)
        previous_state = _state_from_row(previous_row)

        if previous_state == BatchApplyState.APPLIED:
            result = self._build_rejected_result(
                batch_id=batch_id,
                previous_state=previous_state,
                reason="duplicate_apply_blocked",
                message=(
                    f"Batch '{batch_id}' has already been applied and is in terminal state. "
                    "Re-application is blocked to prevent duplicate effects."
                ),
                timestamp=now_iso,
                previous_result=_result_from_row(previous_row),
            )
            self._record_rejected_attempt(
                batch_id=batch_id,
                previous_state=previous_state,
                reason="duplicate_apply_blocked",
                result=result,
            )
            return result

        if previous_state == BatchApplyState.REJECTED:
            result = self._build_rejected_result(
                batch_id=batch_id,
                previous_state=previous_state,
                reason="duplicate_apply_blocked",
                message=f"Batch '{batch_id}' was previously rejected and cannot be re-applied.",
                timestamp=now_iso,
                previous_result=_result_from_row(previous_row),
            )
            self._record_rejected_attempt(
                batch_id=batch_id,
                previous_state=previous_state,
                reason="duplicate_apply_blocked",
                result=result,
            )
            return result

        if previous_state == BatchApplyState.APPLYING:
            result = self._build_rejected_result(
                batch_id=batch_id,
                previous_state=previous_state,
                reason="unsafe_state",
                message=(
                    f"Batch '{batch_id}' is currently in APPLYING state. Cannot re-enter apply."
                ),
                timestamp=now_iso,
                previous_result=_result_from_row(previous_row),
            )
            self._record_rejected_attempt(
                batch_id=batch_id,
                previous_state=previous_state,
                reason="unsafe_state",
                result=result,
            )
            return result

        self._transition_to_applying(
            batch_id=batch_id,
            previous_state=previous_state,
            idempotency_key=idempotency_key,
            timestamp=now_iso,
        )

        try:
            results = apply_decisions(
                items,
                decisions,
                evidence_refs_by_queue_item=evidence_refs_by_queue_item,
                require_evidence_ref=require_evidence_ref,
            )
        except ApplyConflictError as exc:
            result = self._build_failed_result(
                batch_id=batch_id,
                results=(),
                error_message=str(exc),
                timestamp=now_iso,
            )
            self._transition_to_final_state(
                batch_id=batch_id,
                state=BatchApplyState.FAILED,
                reason="apply_failed",
                result=result,
            )
            return result
        except Exception as exc:
            error_message = f"Unexpected batch apply error: {type(exc).__name__}: {exc}"
            result = self._build_failed_result(
                batch_id=batch_id,
                results=(),
                error_message=error_message,
                timestamp=now_iso,
            )
            self._transition_to_final_state(
                batch_id=batch_id,
                state=BatchApplyState.FAILED,
                reason="apply_failed",
                result=result,
            )
            raise

        success_count = sum(1 for r in results if r.success)
        fail_count = sum(1 for r in results if not r.success)
        total = len(results)

        if fail_count > 0 or total == 0:
            error_message = (
                f"Batch apply partially or fully failed: {fail_count} of {total} items failed"
                if total > 0
                else "Batch apply produced no results"
            )
            result = self._build_failed_result(
                batch_id=batch_id,
                results=tuple(results),
                error_message=error_message,
                timestamp=now_iso,
            )
            self._transition_to_final_state(
                batch_id=batch_id,
                state=BatchApplyState.FAILED,
                reason="apply_failed",
                result=result,
            )
            return result

        audit = _build_batch_audit(
            batch_id=batch_id,
            previous_state=BatchApplyState.APPLYING,
            state=BatchApplyState.APPLIED,
            reason="applied_successfully",
            total=total,
            success_count=success_count,
            fail_count=fail_count,
            evidence_refs_by_queue_item=evidence_refs_by_queue_item,
        )
        result = BatchApplyResult(
            batch_id=batch_id,
            state=BatchApplyState.APPLIED,
            results=tuple(results),
            audit_metadata=audit,
            error_message=None,
            batch_applied_at=now_iso,
            idempotent=False,
        )
        self._transition_to_final_state(
            batch_id=batch_id,
            state=BatchApplyState.APPLIED,
            reason="applied_successfully",
            result=result,
        )
        return result

    def get_batch_state(self, batch_id: str) -> BatchApplyState | None:
        """Return the current persisted state of a batch, or None."""
        return _state_from_row(self._get_batch_row(batch_id))

    def is_terminal(self, batch_id: str) -> bool:
        """Return True if the persisted batch state is terminal."""
        state = self.get_batch_state(batch_id)
        return state is not None and state.is_terminal

    def get_batch_result(self, batch_id: str) -> BatchApplyResult | None:
        """Return the persisted batch result, if a result JSON exists."""
        return _result_from_row(self._get_batch_row(batch_id))

    def list_transitions(self, batch_id: str) -> list[sqlite3.Row]:
        """Return transition audit rows for a batch ordered by insertion."""
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_apply_batch_state_transitions
            WHERE batch_id = ?
            ORDER BY id ASC
            """,
            (batch_id,),
        ).fetchall()

    def reset(self) -> None:
        """Clear persisted batch state tables on the provided connection."""
        self._conn.execute("DELETE FROM reconciliation_apply_batch_state_transitions")
        self._conn.execute("DELETE FROM reconciliation_apply_batches")
        self._conn.commit()

    def _transition_to_applying(
        self,
        *,
        batch_id: str,
        previous_state: BatchApplyState | None,
        idempotency_key: str | None,
        timestamp: str,
    ) -> None:
        reason = "apply_started" if previous_state is None else "apply_retry_started"
        metadata = {
            "batch_id": batch_id,
            "state": BatchApplyState.APPLYING.value,
            "previous_state": previous_state.value if previous_state else None,
            "reason": reason,
            "transition_at": timestamp,
        }
        if previous_state is None:
            self._conn.execute(
                """
                INSERT INTO reconciliation_apply_batches (
                    batch_id, current_state, idempotency_key
                ) VALUES (?, ?, ?)
                """,
                (batch_id, BatchApplyState.APPLYING.value, idempotency_key),
            )
        else:
            self._conn.execute(
                """
                UPDATE reconciliation_apply_batches
                SET current_state = ?,
                    error_message = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE batch_id = ?
                """,
                (BatchApplyState.APPLYING.value, batch_id),
            )
        self._insert_transition(
            batch_id=batch_id,
            previous_state=previous_state,
            new_state=BatchApplyState.APPLYING,
            reason=reason,
            audit_metadata=metadata,
        )
        self._conn.commit()

    def _transition_to_final_state(
        self,
        *,
        batch_id: str,
        state: BatchApplyState,
        reason: str,
        result: BatchApplyResult,
    ) -> None:
        self._conn.execute(
            """
            UPDATE reconciliation_apply_batches
            SET current_state = ?,
                applied_result_json = ?,
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE batch_id = ?
            """,
            (
                state.value,
                _serialize_batch_result(result),
                result.error_message,
                batch_id,
            ),
        )
        self._insert_transition(
            batch_id=batch_id,
            previous_state=BatchApplyState.APPLYING,
            new_state=state,
            reason=reason,
            audit_metadata=result.audit_metadata,
        )
        self._conn.commit()

    def _record_rejected_attempt(
        self,
        *,
        batch_id: str,
        previous_state: BatchApplyState,
        reason: str,
        result: BatchApplyResult,
    ) -> None:
        self._insert_transition(
            batch_id=batch_id,
            previous_state=previous_state,
            new_state=BatchApplyState.REJECTED,
            reason=reason,
            audit_metadata=result.audit_metadata,
        )
        self._conn.commit()

    def _insert_transition(
        self,
        *,
        batch_id: str,
        previous_state: BatchApplyState | None,
        new_state: BatchApplyState,
        reason: str,
        audit_metadata: dict[str, Any],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO reconciliation_apply_batch_state_transitions (
                batch_id, previous_state, new_state, reason, audit_metadata_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                previous_state.value if previous_state is not None else None,
                new_state.value,
                reason,
                _serialize_json(audit_metadata),
            ),
        )

    def _get_batch_row(self, batch_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_apply_batches
            WHERE batch_id = ?
            """,
            (batch_id,),
        ).fetchone()

    def _build_rejected_result(
        self,
        *,
        batch_id: str,
        previous_state: BatchApplyState,
        reason: str,
        message: str,
        timestamp: str,
        previous_result: BatchApplyResult | None,
    ) -> BatchApplyResult:
        return BatchApplyResult(
            batch_id=batch_id,
            state=BatchApplyState.REJECTED,
            results=(previous_result.results if previous_result is not None else ()),
            audit_metadata={
                "batch_id": batch_id,
                "state": BatchApplyState.REJECTED.value,
                "previous_state": previous_state.value,
                "reason": reason,
                "message": message,
                "attempted_at": timestamp,
                "total_items": len(previous_result.results if previous_result else ()),
                "successful_items": previous_result.applied_count if previous_result else 0,
                "failed_items": previous_result.failed_count if previous_result else 0,
                "evidence_refs_tracked": False,
            },
            error_message=message,
            batch_applied_at=timestamp,
            idempotent=True,
        )

    @staticmethod
    def _build_failed_result(
        *,
        batch_id: str,
        results: tuple[ResolutionApplyResult, ...],
        error_message: str,
        timestamp: str,
    ) -> BatchApplyResult:
        total = len(results)
        success_count = sum(1 for r in results if r.success)
        fail_count = total - success_count
        return BatchApplyResult(
            batch_id=batch_id,
            state=BatchApplyState.FAILED,
            results=results,
            audit_metadata={
                "batch_id": batch_id,
                "state": BatchApplyState.FAILED.value,
                "previous_state": BatchApplyState.APPLYING.value,
                "reason": "apply_failed",
                "message": error_message,
                "attempted_at": timestamp,
                "total_items": total,
                "successful_items": success_count,
                "failed_items": fail_count,
                "evidence_refs_tracked": False,
            },
            error_message=error_message,
            batch_applied_at=timestamp,
            idempotent=False,
        )

    def _guard_live_database(self) -> None:
        live_path = LIVE_DB_PATH.resolve()
        rows = self._conn.execute("PRAGMA database_list").fetchall()
        for row in rows:
            raw_path = row["file"]
            if not raw_path:
                continue
            db_path = Path(raw_path).resolve()
            if db_path == live_path:
                raise ValueError(
                    f"Refusing to use live database for apply batch state persistence: {live_path}"
                )


def _state_from_row(row: sqlite3.Row | None) -> BatchApplyState | None:
    if row is None:
        return None
    return BatchApplyState(row["current_state"])


def _result_from_row(row: sqlite3.Row | None) -> BatchApplyResult | None:
    if row is None or row["applied_result_json"] is None:
        return None
    try:
        return _deserialize_batch_result(row["applied_result_json"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SQLiteBatchApplyStateError(
            f"Stored apply batch result is malformed for batch '{row['batch_id']}'"
        ) from exc


def _serialize_batch_result(result: BatchApplyResult) -> str:
    return _serialize_json(
        {
            "batch_id": result.batch_id,
            "state": result.state.value,
            "results": [_resolution_result_to_dict(r) for r in result.results],
            "audit_metadata": result.audit_metadata,
            "error_message": result.error_message,
            "batch_applied_at": result.batch_applied_at,
            "idempotent": result.idempotent,
        }
    )


def _deserialize_batch_result(value: str) -> BatchApplyResult:
    data = json.loads(value)
    return BatchApplyResult(
        batch_id=data["batch_id"],
        state=BatchApplyState(data["state"]),
        results=tuple(_resolution_result_from_dict(r) for r in data.get("results", [])),
        audit_metadata=data.get("audit_metadata", {}),
        error_message=data.get("error_message"),
        batch_applied_at=data.get("batch_applied_at", ""),
        idempotent=bool(data.get("idempotent", False)),
    )


def _resolution_result_to_dict(result: ResolutionApplyResult) -> dict[str, Any]:
    return {
        "apply_id": result.apply_id,
        "decision_id": result.decision_id,
        "queue_item_id": result.queue_item_id,
        "candidate_id": result.candidate_id,
        "action": result.action.value,
        "success": result.success,
        "payload": result.payload,
        "error_message": result.error_message,
        "audit_evidence": result.audit_evidence,
        "applied_at": result.applied_at,
        "reviewer": result.reviewer,
        "note": result.note,
        "statement_reference": result.statement_reference,
        "app_transaction_reference": result.app_transaction_reference,
        "idempotent": result.idempotent,
    }


def _resolution_result_from_dict(data: dict[str, Any]) -> ResolutionApplyResult:
    return ResolutionApplyResult(
        apply_id=data["apply_id"],
        decision_id=data["decision_id"],
        queue_item_id=data["queue_item_id"],
        candidate_id=data.get("candidate_id", ""),
        action=ResolutionAction(data["action"]),
        success=bool(data["success"]),
        payload=data.get("payload", {}),
        error_message=data.get("error_message"),
        audit_evidence=data.get("audit_evidence", {}),
        applied_at=data.get("applied_at", ""),
        reviewer=data.get("reviewer", "human"),
        note=data.get("note", ""),
        statement_reference=data.get("statement_reference"),
        app_transaction_reference=data.get("app_transaction_reference"),
        idempotent=bool(data.get("idempotent", False)),
    )


def _build_batch_audit(
    *,
    batch_id: str,
    previous_state: BatchApplyState,
    state: BatchApplyState,
    reason: str,
    total: int,
    success_count: int,
    fail_count: int,
    evidence_refs_by_queue_item: Mapping[str, tuple[str, ...]] | None,
) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "state": state.value,
        "previous_state": previous_state.value,
        "reason": reason,
        "transition_at": _utc_now(),
        "total_items": total,
        "successful_items": success_count,
        "failed_items": fail_count,
        "evidence_refs_tracked": evidence_refs_by_queue_item is not None,
        "apply_runtime_version": "v1",
        "batch_state_control_version": "sqlite-v1",
    }


def _serialize_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "SQLiteBatchApplyStateError",
    "SQLiteBatchApplyStateManager",
]
