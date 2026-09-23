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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace

from finance_core.application.correction_schema import (
    has_committed_correction,
    verify_correction_schema,
)
from finance_core.reconciliation.models import (
    ResolutionAction,
    ResolutionDecision,
    ResolutionResult,
    ReviewQueueItem,
    validate_resolution_decision,
)
from finance_core.reconciliation.resolution import ResolutionRuntime
from finance_core.reconciliation.source_binding import (
    BoundQueue,
    assert_candidate_matches_bound,
    load_bound_queue,
)


class DuplicateResolutionPersistenceError(Exception):
    """Raised when an existing resolution audit row conflicts with new data."""


@contextmanager
def _owned_write(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        raise RuntimeError("reconciliation requires a connection without caller-owned work")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _guard_successful_resolution(conn: sqlite3.Connection, result: ResolutionResult) -> None:
    if not result.success or result.decision.action not in {
        ResolutionAction.CONFIRM_MATCH,
        ResolutionAction.MARK_DUPLICATE,
    }:
        return
    bound = _bound_successful_classification(conn, result)
    candidate = result.queue_item.candidate
    targets: set[str] = set()
    if candidate.best_app_transaction is not None:
        targets.add(candidate.best_app_transaction.app_txn_id)
    if bound is not None:
        # A confirm decision can still originate from a multi-app candidate.
        # Every frozen source member must be checked before classification.
        targets.update(app.app_txn_id for app in bound.app_transactions)
    elif result.decision.action == ResolutionAction.MARK_DUPLICATE:
        targets.update(app.app_txn_id for app in candidate.all_app_transactions)
    evidence_id = result.audit_evidence.get("app_txn_id")
    if isinstance(evidence_id, str) and evidence_id:
        targets.add(evidence_id)
    if not targets:
        raise ValueError("successful reconciliation has incomplete app target enumeration")
    queue = conn.execute(
        "SELECT app_transaction_ref FROM reconciliation_review_queue WHERE public_id = ?",
        (result.queue_item.queue_item_id,),
    ).fetchone()
    if queue is None or not isinstance(queue[0], str) or not queue[0]:
        raise ValueError("successful reconciliation has no canonical app reference")
    targets.add(queue[0])
    if any(has_committed_correction(conn, target) for target in sorted(targets)):
        raise ValueError("corrected_transaction_requires_versioned_reconciliation")


def _bound_successful_classification(
    conn: sqlite3.Connection, result: ResolutionResult
) -> BoundQueue | None:
    """Bind a successful classification to the frozen 051 queue source."""
    if not result.success or result.decision.action not in {
        ResolutionAction.CONFIRM_MATCH,
        ResolutionAction.MARK_DUPLICATE,
    }:
        return None
    bound = load_bound_queue(conn, result.queue_item.queue_item_id)
    compatible, _ = validate_resolution_decision(
        result.decision.action, result.queue_item.issue_type
    )
    if not compatible:
        raise ValueError("successful reconciliation action is incompatible with issue type")
    if bound is not None:
        assert_candidate_matches_bound(bound, result.queue_item)
        compatible, _ = validate_resolution_decision(result.decision.action, bound.issue_type)
        if not compatible or result.queue_item.issue_type != bound.issue_type:
            raise ValueError("successful reconciliation action conflicts with frozen queue issue")
        if result.decision.queue_item_id != bound.queue_item_id:
            raise ValueError("successful reconciliation decision queue identity changed")
        evidence = result.audit_evidence
        if (
            evidence.get("queue_item_id") != bound.queue_item_id
            or evidence.get("candidate_id") != bound.candidate_id
            or evidence.get("resolution_action") != result.decision.action.value
        ):
            raise ValueError("successful reconciliation evidence source identity changed")
    return bound


def _complete_success_evidence(
    conn: sqlite3.Connection, result: ResolutionResult
) -> ResolutionResult:
    if not result.success or result.decision.action not in {
        ResolutionAction.CONFIRM_MATCH,
        ResolutionAction.MARK_DUPLICATE,
    }:
        return result
    bound = _bound_successful_classification(conn, result)
    candidate = result.queue_item.candidate
    queue = conn.execute(
        "SELECT app_transaction_ref FROM reconciliation_review_queue WHERE public_id = ?",
        (result.queue_item.queue_item_id,),
    ).fetchone()
    canonical = queue[0] if queue is not None else None
    best = candidate.best_app_transaction
    if not isinstance(canonical, str) or not canonical or best is None:
        raise ValueError("successful reconciliation has incomplete canonical app evidence")
    if canonical != best.app_txn_id:
        raise ValueError("successful reconciliation canonical app reference changed")
    evidence = dict(result.audit_evidence)
    if evidence.get("app_txn_id") not in {None, canonical}:
        raise ValueError("successful reconciliation payload target differs from canonical app ref")
    evidence["app_txn_id"] = canonical
    evidence["canonical_app_transaction_ref"] = canonical
    evidence["payload_target_app_txn_id"] = best.app_txn_id
    if result.decision.action == ResolutionAction.MARK_DUPLICATE:
        duplicates = [
            app.app_txn_id
            for app in (
                bound.app_transactions if bound is not None else candidate.all_app_transactions
            )
        ]
        if (
            len(duplicates) < 2
            or len(set(duplicates)) != len(duplicates)
            or canonical not in duplicates
        ):
            raise ValueError("successful duplicate classification has incomplete app targets")
        for key, value in (
            ("duplicate_app_txn_ids", duplicates),
            ("kept_app_txn_id", canonical),
            ("audit_only", True),
        ):
            if key in evidence and evidence[key] != value:
                raise ValueError("successful duplicate evidence conflicts with candidate")
            evidence[key] = value
    return replace(result, audit_evidence=evidence)


def _guard_successful_decision_replay(
    conn: sqlite3.Connection, decision: ResolutionDecision
) -> None:
    if decision.action not in {ResolutionAction.CONFIRM_MATCH, ResolutionAction.MARK_DUPLICATE}:
        return
    if not verify_correction_schema(conn):
        return
    prior = conn.execute(
        "SELECT results.audit_evidence_json, queue.app_transaction_ref, "
        "results.review_queue_public_id "
        "FROM reconciliation_resolution_results AS results "
        "JOIN reconciliation_review_queue AS queue "
        "ON queue.public_id = results.review_queue_public_id "
        "WHERE results.decision_public_id = ? AND results.success = 1",
        (decision.decision_id,),
    ).fetchone()
    if prior is None:
        return
    bound = load_bound_queue(conn, decision.queue_item_id)
    if bound is not None and prior[2] != bound.queue_item_id:
        raise ValueError("successful reconciliation replay queue identity changed")
    try:
        evidence = json.loads(prior[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("successful reconciliation evidence is incomplete") from exc
    if not isinstance(evidence, dict):
        raise ValueError("successful reconciliation evidence is incomplete")
    canonical = prior[1]
    target = evidence.get("app_txn_id")
    if not isinstance(canonical, str) or not canonical or target != canonical:
        raise ValueError("successful reconciliation evidence is incomplete")
    if bound is not None and canonical != bound.app_transaction_ref:
        raise ValueError("successful reconciliation replay source changed")
    targets = {canonical}
    if decision.action == ResolutionAction.MARK_DUPLICATE:
        duplicates = evidence.get("duplicate_app_txn_ids")
        if (
            not isinstance(duplicates, list)
            or len(duplicates) < 2
            or not all(isinstance(value, str) and value for value in duplicates)
            or len(set(duplicates)) != len(duplicates)
            or evidence.get("kept_app_txn_id") != canonical
            or canonical not in duplicates
            or evidence.get("audit_only") is not True
        ):
            raise ValueError("successful duplicate evidence is incomplete")
        if bound is not None and duplicates != [app.app_txn_id for app in bound.app_transactions]:
            raise ValueError("successful duplicate evidence differs from frozen queue source")
        targets.update(duplicates)
    if any(has_committed_correction(conn, ref) for ref in sorted(targets)):
        raise ValueError("corrected_transaction_requires_versioned_reconciliation")


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
        with _owned_write(self._conn):
            _guard_successful_decision_replay(self._conn, decision)
            return self._insert_decision(decision)

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
        with _owned_write(self._conn):
            _guard_successful_resolution(self._conn, result)
            return self._insert_result(_complete_success_evidence(self._conn, result))

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

        with _owned_write(self._conn):
            if result.decision != decision or result.queue_item != item:
                raise ValueError("reconciliation caller and result disagree")
            if result.success and result.decision.action in {
                ResolutionAction.CONFIRM_MATCH,
                ResolutionAction.MARK_DUPLICATE,
            }:
                bound = load_bound_queue(self._conn, item.queue_item_id)
                if bound is not None:
                    assert_candidate_matches_bound(bound, item)
            _guard_successful_resolution(self._conn, result)
            self._insert_decision(decision)
            self._insert_result(_complete_success_evidence(self._conn, result))

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
