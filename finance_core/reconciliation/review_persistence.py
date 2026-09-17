"""Reconciliation Review Queue Persistence v1 -- persists review queue
items into the reconciliation_review_queue table.

All methods operate on an externally-provided ``sqlite3.Connection``.
The caller is responsible for connection lifecycle and ensuring the
reconciliation persistence schema (including migration 007) is already
applied.

Key design properties:
- Never opens or modifies database/finance.db.
- Deterministic JSON serialization for reason_codes and evidence.
- Decimal-safe: amounts and confidence scores are stored as text.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence

from finance_core.reconciliation.models import (
    ReviewQueueItem,
)


class ReviewQueuePersistence:
    """Persists ReviewQueueItem objects to the reconciliation_review_queue
    table and provides read-back methods.

    Usage::

        conn = connect_sqlite("/tmp/review_demo.db")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        # ... apply migration 007 ...
        rqp = ReviewQueuePersistence(conn)
        rqp.persist_review_queue(items, run_public_id="run-001")
        pending = rqp.list_pending()
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def persist_review_queue(
        self,
        items: Sequence[ReviewQueueItem],
        *,
        run_public_id: str = "",
    ) -> int:
        """Insert review queue items into reconciliation_review_queue.

        Returns the number of rows inserted.
        """
        rows = [
            (
                item.queue_item_id,
                run_public_id,
                item.candidate.candidate_id,
                item.issue_type.value,
                item.suggested_action.value,
                item.priority,
                _statement_ref(item),
                _app_ref(item),
                str(item.candidate.confidence_score),
                _serialize_reason_codes(item),
                _serialize_evidence(item),
            )
            for item in items
        ]

        self._conn.executemany(
            """
            INSERT INTO reconciliation_review_queue (
                public_id, run_public_id, candidate_id, issue_type,
                suggested_action, priority, statement_transaction_ref,
                app_transaction_ref, confidence_score,
                reason_codes_json, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self._conn.commit()
        return len(rows)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_pending(self) -> list[sqlite3.Row]:
        """Return all review queue rows with status 'pending'."""
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_review_queue
            WHERE status = 'pending'
            ORDER BY priority ASC, id ASC
            """
        ).fetchall()

    def list_by_status(self, status: str) -> list[sqlite3.Row]:
        """Return review queue rows filtered by status."""
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_review_queue
            WHERE status = ?
            ORDER BY priority ASC, id ASC
            """,
            (status,),
        ).fetchall()

    def list_by_issue_type(self, issue_type: str) -> list[sqlite3.Row]:
        """Return review queue rows filtered by issue_type."""
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_review_queue
            WHERE issue_type = ?
            ORDER BY priority ASC, id ASC
            """,
            (issue_type,),
        ).fetchall()

    def get_by_public_id(self, public_id: str) -> sqlite3.Row | None:
        """Return a single review queue row by public_id, or None."""
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_review_queue
            WHERE public_id = ?
            """,
            (public_id,),
        ).fetchone()

    def update_status(self, public_id: str, status: str) -> bool:
        """Update the status of a review queue item.

        Returns True if a row was updated, False otherwise.
        """
        cursor = self._conn.execute(
            """
            UPDATE reconciliation_review_queue
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE public_id = ?
            """,
            (status, public_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Counts
    # ------------------------------------------------------------------

    def count_all(self) -> int:
        """Return total rows in the review queue."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM reconciliation_review_queue"
        ).fetchone()
        return row["cnt"] if row else 0

    def count_by_status(self, status: str) -> int:
        """Return count of rows with a given status."""
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM reconciliation_review_queue
            WHERE status = ?
            """,
            (status,),
        ).fetchone()
        return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# Private serialization helpers
# ---------------------------------------------------------------------------

_EMPTY_DICT: dict[str, Any] = {}
_EMPTY_LIST: list[str] = []


def _serialize_reason_codes(item: ReviewQueueItem) -> str:
    codes = [r.value for r in item.reason_codes]
    return json.dumps(codes, sort_keys=True, separators=(",", ":"))


def _serialize_evidence(item: ReviewQueueItem) -> str:
    cand = item.candidate
    evidence: dict[str, Any] = {
        "candidate_id": cand.candidate_id,
        "match_status": cand.match_status.value,
        "issue_type": cand.issue_type.value,
        "confidence_score": str(cand.confidence_score),
    }
    if cand.statement is not None:
        stmt = cand.statement
        evidence["statement_merchant"] = stmt.merchant_raw
        evidence["statement_amount"] = str(stmt.amount) if stmt.amount is not None else None
        evidence["statement_currency"] = stmt.currency
        if stmt.transaction_date:
            evidence["statement_txn_date"] = stmt.transaction_date.isoformat()
        if stmt.posted_date:
            evidence["statement_posted_date"] = stmt.posted_date.isoformat()
    if cand.best_app_transaction is not None:
        app = cand.best_app_transaction
        evidence["app_txn_id"] = app.app_txn_id
        evidence["app_merchant"] = app.merchant
        evidence["app_amount"] = str(app.amount)
        evidence["app_currency"] = app.currency
        evidence["app_txn_date"] = app.transaction_date.isoformat()
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"))


def _statement_ref(item: ReviewQueueItem) -> str | None:
    stmt = item.candidate.statement
    if stmt is None:
        return None
    return stmt.statement_row_reference or stmt.merchant_raw


def _app_ref(item: ReviewQueueItem) -> str | None:
    app = item.candidate.best_app_transaction
    if app is None:
        return None
    return app.app_txn_id


__all__ = [
    "ReviewQueuePersistence",
]
