"""Reconciliation Review Queue v1 -- read-only helpers for inspecting
persisted match results and reconciliation run summaries.

All methods are **read-only** and use the existing
``reconciliation_match_results`` and ``reconciliation_runs`` tables.
No data is created, updated, or deleted.

This module wraps an externally-provided ``sqlite3.Connection``.  The
caller is responsible for connection lifecycle and ensuring the
reconciliation persistence schema is already migrated.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from finance_core.reconciliation.models import (
    IssueType,
    ReconciliationCandidate,
    ReconciliationReviewEvidence,
    ReconciliationSummary,
    ReviewPriority,
    ReviewQueueItem,
    SuggestedAction,
    priority_for_issue,
    review_priority_for_issue,
    sort_key_for_review_item,
    suggested_action_for_issue,
)

# ---------------------------------------------------------------------------
# View models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewQueueEntry:
    """A single entry in the review queue -- one match result that needs
    human attention."""

    id: int
    public_id: str
    run_id: int
    run_public_id: str
    statement_transaction_id: int
    match_status: str
    reason_codes: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    internal_candidate_id: str | None = None
    amount_delta: Decimal | None = None
    date_delta_days: int | None = None
    merchant_similarity: float | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class RunSummaryView:
    """Aggregated summary for a single reconciliation run."""

    run_id: int
    run_public_id: str
    run_status: str
    batch_id: int | None = None
    total_match_results: int = 0
    matched_count: int = 0
    no_match_count: int = 0
    amount_mismatch_count: int = 0
    currency_mismatch_count: int = 0
    date_mismatch_count: int = 0
    merchant_mismatch_count: int = 0
    possible_duplicate_count: int = 0
    ambiguous_count: int = 0
    needs_review_count: int = 0
    started_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True)
class StatementContextView:
    """Full context for a single statement transaction including its match
    result and audit trail."""

    statement_transaction_id: int
    statement_public_id: str
    merchant_raw: str
    amount: Decimal
    currency: str
    transaction_date: str | None = None
    posted_date: str | None = None
    match_status: str | None = None
    match_result_public_id: str | None = None
    internal_candidate_id: str | None = None
    reason_codes: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    batch_id: int | None = None


@dataclass(frozen=True)
class MatchAuditView:
    """Detailed audit view for a single match result, including both
    statement and candidate evidence."""

    match_result_id: int
    match_public_id: str
    run_id: int
    run_public_id: str
    statement_transaction_id: int
    match_status: str
    reason_codes: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    internal_candidate_id: str | None = None
    amount_delta: Decimal | None = None
    date_delta_days: int | None = None
    merchant_similarity: float | None = None
    needs_review: bool = False
    created_at: str | None = None


# ---------------------------------------------------------------------------
# Review Queue
# ---------------------------------------------------------------------------


class ReconciliationReviewQueue:
    """Read-only review queue that inspects persisted match results.

    All methods query the existing reconciliation tables.  No data is
    created, updated, or deleted.  This class does not open, create, or
    modify any database file.

    Usage::

        conn = connect_sqlite("...")
        queue = ReconciliationReviewQueue(conn)
        entries = queue.get_review_required()
        summary = queue.get_run_summary(run_id=1)
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn

    # ------------------------------------------------------------------
    # Review queue
    # ------------------------------------------------------------------

    def get_review_required(
        self,
        run_id: int | None = None,
    ) -> list[ReviewQueueEntry]:
        """Return all match results flagged for human review.

        When ``run_id`` is ``None``, returns review-required entries across
        all runs.
        """
        if run_id is not None:
            rows = self._conn.execute(
                """
                SELECT
                  mr.*,
                  rr.public_id AS run_public_id
                FROM reconciliation_match_results mr
                JOIN reconciliation_runs rr ON mr.run_id = rr.id
                WHERE mr.needs_review = 1 AND mr.run_id = ?
                ORDER BY mr.id ASC
                """,
                (run_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT
                  mr.*,
                  rr.public_id AS run_public_id
                FROM reconciliation_match_results mr
                JOIN reconciliation_runs rr ON mr.run_id = rr.id
                WHERE mr.needs_review = 1
                ORDER BY mr.id ASC
                """,
            ).fetchall()
        return [_row_to_review_entry(r) for r in rows]

    def get_unmatched(
        self,
        run_id: int,
    ) -> list[ReviewQueueEntry]:
        """Return match results where status is not ``matched``."""
        rows = self._conn.execute(
            """
            SELECT
              mr.*,
              rr.public_id AS run_public_id
            FROM reconciliation_match_results mr
            JOIN reconciliation_runs rr ON mr.run_id = rr.id
            WHERE mr.run_id = ? AND mr.match_status != 'matched'
            ORDER BY mr.id ASC
            """,
            (run_id,),
        ).fetchall()
        return [_row_to_review_entry(r) for r in rows]

    def get_amount_mismatches(
        self,
        run_id: int | None = None,
    ) -> list[ReviewQueueEntry]:
        """Return match results with amount mismatch status."""
        if run_id is not None:
            rows = self._conn.execute(
                """
                SELECT
                  mr.*,
                  rr.public_id AS run_public_id
                FROM reconciliation_match_results mr
                JOIN reconciliation_runs rr ON mr.run_id = rr.id
                WHERE mr.match_status = 'amount_mismatch' AND mr.run_id = ?
                ORDER BY mr.id ASC
                """,
                (run_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT
                  mr.*,
                  rr.public_id AS run_public_id
                FROM reconciliation_match_results mr
                JOIN reconciliation_runs rr ON mr.run_id = rr.id
                WHERE mr.match_status = 'amount_mismatch'
                ORDER BY mr.id ASC
                """,
            ).fetchall()
        return [_row_to_review_entry(r) for r in rows]

    def get_ambiguous(
        self,
        run_id: int | None = None,
    ) -> list[ReviewQueueEntry]:
        """Return match results with ambiguous or possible_duplicate status."""
        if run_id is not None:
            rows = self._conn.execute(
                """
                SELECT
                  mr.*,
                  rr.public_id AS run_public_id
                FROM reconciliation_match_results mr
                JOIN reconciliation_runs rr ON mr.run_id = rr.id
                WHERE mr.match_status IN ('ambiguous', 'possible_duplicate')
                  AND mr.run_id = ?
                ORDER BY mr.id ASC
                """,
                (run_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT
                  mr.*,
                  rr.public_id AS run_public_id
                FROM reconciliation_match_results mr
                JOIN reconciliation_runs rr ON mr.run_id = rr.id
                WHERE mr.match_status IN ('ambiguous', 'possible_duplicate')
                ORDER BY mr.id ASC
                """,
            ).fetchall()
        return [_row_to_review_entry(r) for r in rows]

    # ------------------------------------------------------------------
    # Run summary
    # ------------------------------------------------------------------

    def get_run_summary(self, run_id: int) -> RunSummaryView | None:
        """Return an aggregated summary for a single reconciliation run."""
        run_row = self._conn.execute(
            """
            SELECT * FROM reconciliation_runs WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if run_row is None:
            return None

        status_counts = self._conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN match_status = 'matched' THEN 1 ELSE 0 END) AS matched,
              SUM(CASE WHEN match_status = 'no_match' THEN 1 ELSE 0 END) AS no_match,
              SUM(CASE WHEN match_status = 'amount_mismatch' THEN 1 ELSE 0 END) AS amount_mismatch,
              SUM(CASE WHEN match_status = 'currency_mismatch' THEN 1 ELSE 0 END)
                AS currency_mismatch,
              SUM(CASE WHEN match_status = 'date_mismatch' THEN 1 ELSE 0 END) AS date_mismatch,
              SUM(CASE WHEN match_status = 'merchant_mismatch' THEN 1 ELSE 0 END)
                AS merchant_mismatch,
              SUM(CASE WHEN match_status = 'possible_duplicate' THEN 1 ELSE 0 END)
                AS possible_duplicate,
              SUM(CASE WHEN match_status = 'ambiguous' THEN 1 ELSE 0 END) AS ambiguous,
              SUM(CASE WHEN needs_review = 1 THEN 1 ELSE 0 END) AS needs_review
            FROM reconciliation_match_results
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

        return RunSummaryView(
            run_id=run_row["id"],
            run_public_id=run_row["public_id"],
            run_status=run_row["run_status"],
            batch_id=run_row["batch_id"],
            total_match_results=_i(status_counts, "total"),
            matched_count=_i(status_counts, "matched"),
            no_match_count=_i(status_counts, "no_match"),
            amount_mismatch_count=_i(status_counts, "amount_mismatch"),
            currency_mismatch_count=_i(status_counts, "currency_mismatch"),
            date_mismatch_count=_i(status_counts, "date_mismatch"),
            merchant_mismatch_count=_i(status_counts, "merchant_mismatch"),
            possible_duplicate_count=_i(status_counts, "possible_duplicate"),
            ambiguous_count=_i(status_counts, "ambiguous"),
            needs_review_count=_i(status_counts, "needs_review"),
            started_at=run_row["started_at"],
            completed_at=run_row["completed_at"],
        )

    def get_all_run_summaries(self) -> list[RunSummaryView]:
        """Return aggregated summaries for all reconciliation runs."""
        run_rows = self._conn.execute(
            """
            SELECT id FROM reconciliation_runs ORDER BY id ASC
            """
        ).fetchall()
        summaries: list[RunSummaryView] = []
        for row in run_rows:
            summary = self.get_run_summary(row["id"])
            if summary is not None:
                summaries.append(summary)
        return summaries

    # ------------------------------------------------------------------
    # Statement context
    # ------------------------------------------------------------------

    def get_statement_context(
        self,
        run_id: int,
    ) -> list[StatementContextView]:
        """Return each statement transaction in a run with its match outcome.

        This is the primary debugging / audit view: every statement row
        in the batch is shown alongside its match result.
        """
        rows = self._conn.execute(
            """
            SELECT
              st.id AS stmt_id,
              st.public_id AS stmt_public_id,
              st.merchant_raw,
              st.amount,
              st.currency,
              st.transaction_date,
              st.posted_date,
              st.batch_id,
              mr.id AS mr_id,
              mr.public_id AS mr_public_id,
              mr.match_status,
              mr.internal_candidate_id,
              mr.reason_codes_json,
              mr.evidence_json
            FROM statement_transactions st
            JOIN reconciliation_match_results mr ON st.id = mr.statement_transaction_id
            WHERE mr.run_id = ?
            ORDER BY st.id ASC
            """,
            (run_id,),
        ).fetchall()
        return [_row_to_context_view(r) for r in rows]

    # ------------------------------------------------------------------
    # Match audit
    # ------------------------------------------------------------------

    def get_match_audit(
        self,
        match_result_id: int,
    ) -> MatchAuditView | None:
        """Return a detailed audit view for a single match result."""
        row = self._conn.execute(
            """
            SELECT
              mr.*,
              rr.public_id AS run_public_id
            FROM reconciliation_match_results mr
            JOIN reconciliation_runs rr ON mr.run_id = rr.id
            WHERE mr.id = ?
            """,
            (match_result_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_audit_view(row)

    def get_match_audits_for_run(
        self,
        run_id: int,
    ) -> list[MatchAuditView]:
        """Return detailed audit views for every match result in a run."""
        rows = self._conn.execute(
            """
            SELECT
              mr.*,
              rr.public_id AS run_public_id
            FROM reconciliation_match_results mr
            JOIN reconciliation_runs rr ON mr.run_id = rr.id
            WHERE mr.run_id = ?
            ORDER BY mr.id ASC
            """,
            (run_id,),
        ).fetchall()
        return [_row_to_audit_view(r) for r in rows]


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------


def _row_to_review_entry(row: sqlite3.Row) -> ReviewQueueEntry:
    amount_delta_raw = row["amount_delta"]
    return ReviewQueueEntry(
        id=row["id"],
        public_id=row["public_id"],
        run_id=row["run_id"],
        run_public_id=row["run_public_id"],
        statement_transaction_id=row["statement_transaction_id"],
        match_status=row["match_status"],
        reason_codes=json.loads(row["reason_codes_json"] or "[]"),
        evidence=json.loads(row["evidence_json"] or "{}"),
        internal_candidate_id=row["internal_candidate_id"],
        amount_delta=Decimal(str(amount_delta_raw)) if amount_delta_raw is not None else None,
        date_delta_days=row["date_delta_days"],
        merchant_similarity=row["merchant_similarity"],
        created_at=row["created_at"],
    )


def _row_to_context_view(row: sqlite3.Row) -> StatementContextView:
    return StatementContextView(
        statement_transaction_id=row["stmt_id"],
        statement_public_id=row["stmt_public_id"],
        merchant_raw=row["merchant_raw"],
        amount=Decimal(str(row["amount"])),
        currency=row["currency"],
        transaction_date=row["transaction_date"],
        posted_date=row["posted_date"],
        match_status=row["match_status"],
        match_result_public_id=row["mr_public_id"],
        internal_candidate_id=row["internal_candidate_id"],
        reason_codes=json.loads(row["reason_codes_json"] or "[]"),
        evidence=json.loads(row["evidence_json"] or "{}"),
        batch_id=row["batch_id"],
    )


def _row_to_audit_view(row: sqlite3.Row) -> MatchAuditView:
    amount_delta_raw = row["amount_delta"]
    return MatchAuditView(
        match_result_id=row["id"],
        match_public_id=row["public_id"],
        run_id=row["run_id"],
        run_public_id=row["run_public_id"],
        statement_transaction_id=row["statement_transaction_id"],
        match_status=row["match_status"],
        reason_codes=json.loads(row["reason_codes_json"] or "[]"),
        evidence=json.loads(row["evidence_json"] or "{}"),
        internal_candidate_id=row["internal_candidate_id"],
        amount_delta=Decimal(str(amount_delta_raw)) if amount_delta_raw is not None else None,
        date_delta_days=row["date_delta_days"],
        merchant_similarity=row["merchant_similarity"],
        needs_review=bool(row["needs_review"]),
        created_at=row["created_at"],
    )


def _i(row: sqlite3.Row | None, col: str) -> int:
    """Safely extract an integer from a row, defaulting to 0."""
    if row is None:
        return 0
    v = row[col]
    return int(v) if v is not None else 0


__all__ = [
    "ReconciliationReviewQueue",
    "ReviewQueueEntry",
    "RunSummaryView",
    "StatementContextView",
    "MatchAuditView",
]

# ============================================================================
# In-Memory Review Queue Generator (Review Queue + Resolution v1)
# ============================================================================
"""The following classes and functions implement the in-memory review queue
generator.  They operate on ``ReconciliationCandidate`` objects produced by
the ``matching.py`` batch engine (not on database rows).

These exist alongside the DB-backed ``ReconciliationReviewQueue`` above
and share the same module for discoverability.
"""


def generate_review_queue(
    candidates: list[ReconciliationCandidate],
    *,
    run_label: str = "",
) -> tuple[list[ReviewQueueItem], ReconciliationSummary]:
    """Generate a review queue from a list of reconciliation candidates.

    Returns both the list of enriched ``ReviewQueueItem`` objects and a
    ``ReconciliationSummary`` with aggregate counts and priority-tier
    groupings.

    Parameters
    ----------
    candidates:
        Reconciliation candidates produced by the batch matching engine.
    run_label:
        Optional label prepended to queue item IDs for identifiability.

    Returns
    -------
    tuple[list[ReviewQueueItem], ReconciliationSummary]
    """
    queue_items: list[ReviewQueueItem] = []

    # Count total unique statement and app transactions
    stmt_ids: set[str] = set()
    app_ids: set[str] = set()
    for c in candidates:
        if c.issue_type != IssueType.MISSING_IN_STATEMENT:
            stmt_ids.add(c.candidate_id)
        if c.best_app_transaction is not None:
            app_ids.add(c.best_app_transaction.app_txn_id)
        for a in c.all_app_transactions:
            app_ids.add(a.app_txn_id)

    counts: dict[str, int] = {}

    for c in candidates:
        issue_type = c.issue_type
        suggested = suggested_action_for_issue(issue_type)
        rp = review_priority_for_issue(issue_type)
        is_review = issue_type != IssueType.MATCHED

        # Reconstruct candidate with review_priority and is_review_required
        if c.review_priority != rp or c.is_review_required != is_review:
            c = dataclasses.replace(c, review_priority=rp, is_review_required=is_review)

        # Build enhanced evidence_summary with statement/app amounts, dates, delta, merchant
        evidence_parts: list[str] = []
        if c.evidence is not None:
            e = c.evidence
            # Statement side
            if e.statement_amount is not None:
                stmt_amt_str = f"Stmt SGD {e.statement_amount}"
                if e.statement_txn_date is not None:
                    stmt_amt_str += f" on {e.statement_txn_date.isoformat()}"
                elif e.statement_posted_date is not None:
                    stmt_amt_str += f" posted {e.statement_posted_date.isoformat()}"
                evidence_parts.append(stmt_amt_str)
            # App side
            app_parts: list[str] = []
            if e.candidate_amount is not None:
                app_parts.append(f"SGD {e.candidate_amount}")
            if e.candidate_txn_date is not None:
                app_parts.append(f"on {e.candidate_txn_date.isoformat()}")
            if e.candidate_merchant:
                app_parts.append(f"({e.candidate_merchant})")
            if app_parts:
                evidence_parts.append("App " + " ".join(app_parts))
            # Amount delta
            if e.statement_amount is not None and e.candidate_amount is not None:
                delta = e.statement_amount - e.candidate_amount
                if delta != 0:
                    evidence_parts.append(f"Amount delta {delta}")
            # Date delta
            if e.date_delta_days is not None:
                evidence_parts.append(f"Date delta {e.date_delta_days}d")
            # Merchant similarity
            if e.merchant_similarity is not None and e.merchant_similarity > 0:
                evidence_parts.append(f"Merchant sim {e.merchant_similarity:.2f}")
            # Candidate count
            if e.candidate_count > 1:
                evidence_parts.append(f"{e.candidate_count} candidates")
        evidence_summary = "; ".join(evidence_parts) if evidence_parts else "no evidence"

        prefix = f"{run_label}-" if run_label else ""
        qid = f"{prefix}q-{len(queue_items):03d}"

        structured = build_structured_evidence(c, suggested=suggested)

        item = ReviewQueueItem(
            queue_item_id=qid,
            candidate=c,
            issue_type=issue_type,
            suggested_action=suggested,
            reason_codes=c.reason_codes,
            evidence_summary=evidence_summary,
            structured_evidence=structured,
            priority=priority_for_issue(issue_type),
        )

        queue_items.append(item)

        key = issue_type.value
        counts[key] = counts.get(key, 0) + 1

    # Sort all items deterministically by (priority, date, merchant, amount)
    queue_items.sort(key=sort_key_for_review_item)

    # Separate matched vs review items (sorted order preserved)
    matched_items = [q for q in queue_items if q.issue_type == IssueType.MATCHED]
    review_items = [q for q in queue_items if q.issue_type != IssueType.MATCHED]

    # Priority tier counts
    high_items = [q for q in queue_items if q.candidate.review_priority == ReviewPriority.HIGH]
    medium_items = [q for q in queue_items if q.candidate.review_priority == ReviewPriority.MEDIUM]
    low_items = [q for q in queue_items if q.candidate.review_priority == ReviewPriority.LOW]

    review_required_count = sum(1 for q in queue_items if q.candidate.is_review_required)

    summary = ReconciliationSummary(
        total_statement_transactions=len(stmt_ids),
        total_app_transactions=len(app_ids),
        matched_count=counts.get("matched", 0),
        needs_review_count=sum(v for k, v in counts.items() if k != IssueType.MATCHED.value),
        review_required_count=review_required_count,
        missing_in_app_count=counts.get("missing_in_app", 0),
        missing_in_statement_count=counts.get("missing_in_statement", 0),
        amount_mismatch_count=counts.get("amount_mismatch", 0),
        currency_mismatch_count=counts.get("currency_mismatch", 0),
        possible_duplicate_count=counts.get("possible_duplicate", 0),
        matched_items=matched_items,
        review_items=review_items,
        priority_items=review_items,
        high_priority_count=len(high_items),
        medium_priority_count=len(medium_items),
        low_priority_count=len(low_items),
        high_priority_items=high_items,
        medium_priority_items=medium_items,
    )

    return queue_items, summary


class ReviewQueueGenerator:
    """In-memory review queue generator that transforms reconciliation
    candidates into review queue items.

    Usage::

        gen = ReviewQueueGenerator()
        items, summary = gen.generate(candidates)
    """

    def __init__(self, run_label: str = "") -> None:
        self._run_label = run_label

    def generate(
        self, candidates: list[ReconciliationCandidate]
    ) -> tuple[list[ReviewQueueItem], ReconciliationSummary]:
        """Generate review queue items and summary from candidates."""
        return generate_review_queue(candidates, run_label=self._run_label)


# ---------------------------------------------------------------------------
# Structured evidence builder
# ---------------------------------------------------------------------------


def build_structured_evidence(
    candidate: ReconciliationCandidate,
    *,
    suggested: SuggestedAction,
) -> ReconciliationReviewEvidence:
    """Build deterministic structured evidence from a reconciliation candidate.

    Derives every field from ``MatchEvidence``, ``StatementTransaction``,
    and the mapped issue type / priority / suggested action -- never from
    AI output.

    When ``MatchEvidence`` fields are ``None`` (e.g. for non-matched cases),
    the builder falls back to the ``StatementTransaction`` or
    ``AppTransaction`` directly, so structured evidence is always as rich
    as possible regardless of match status.
    """
    e = candidate.evidence
    stmt = candidate.statement
    best = candidate.best_app_transaction

    # Statement side -- prefer evidence, fall back to statement
    statement_reference = stmt.statement_row_reference
    statement_transaction_date = stmt.transaction_date
    statement_posted_date = stmt.posted_date
    statement_merchant = stmt.merchant_raw
    statement_amount = (
        e.statement_amount if (e is not None and e.statement_amount is not None) else stmt.amount
    )
    statement_currency = (
        e.statement_currency
        if (e is not None and e.statement_currency is not None)
        else stmt.currency
    )

    # App side -- prefer evidence, fall back to best_app_transaction
    app_transaction_id = best.app_txn_id if best is not None else None
    app_transaction_date = (
        e.candidate_txn_date
        if (e is not None and e.candidate_txn_date is not None)
        else (best.transaction_date if best is not None else None)
    )
    app_merchant = (
        e.candidate_merchant
        if (e is not None and e.candidate_merchant is not None)
        else (best.merchant if best is not None else None)
    )
    app_amount = (
        e.candidate_amount
        if (e is not None and e.candidate_amount is not None)
        else (best.amount if best is not None else None)
    )
    app_currency = (
        e.candidate_currency
        if (e is not None and e.candidate_currency is not None)
        else (best.currency if best is not None else None)
    )

    # Computed deltas -- these need both sides to be meaningful
    amount_delta: Decimal | None = None
    if statement_amount is not None and app_amount is not None:
        amount_delta = statement_amount - app_amount

    date_delta_days = (
        e.date_delta_days if (e is not None and e.date_delta_days is not None) else None
    )
    merchant_similarity = (
        e.merchant_similarity if (e is not None and e.merchant_similarity is not None) else None
    )
    candidate_count = e.candidate_count if e is not None else 0

    # Classification fields (from candidate, not re-derived)
    reason_codes = candidate.reason_codes
    issue_type = candidate.issue_type
    review_priority = candidate.review_priority
    confidence_score = candidate.confidence_score

    return ReconciliationReviewEvidence(
        statement_reference=statement_reference,
        statement_transaction_date=statement_transaction_date,
        statement_posted_date=statement_posted_date,
        statement_merchant=statement_merchant,
        statement_amount=statement_amount,
        statement_currency=statement_currency,
        app_transaction_id=app_transaction_id,
        app_transaction_date=app_transaction_date,
        app_merchant=app_merchant,
        app_amount=app_amount,
        app_currency=app_currency,
        amount_delta=amount_delta,
        date_delta_days=date_delta_days,
        merchant_similarity=merchant_similarity,
        candidate_count=candidate_count,
        reason_codes=reason_codes,
        issue_type=issue_type,
        review_priority=review_priority,
        suggested_action=suggested,
        confidence_score=confidence_score,
    )


# Extended __all__
__all__ = __all__ + [
    "ReviewQueueGenerator",
    "build_structured_evidence",
    "generate_review_queue",
]


# ============================================================================
# Reconciliation Review Queue Preview v1
# ============================================================================
"""The following dataclass and function implement a read-only *preview* of the
reconciliation review queue.  They summarise persisted review items into
stable, testable queue records for human inspection.

They operate on an externally-provided ``sqlite3.Connection`` that already has
the reconciliation persistence schema migrated (migrations 006, 007, 011,
013, 014).  The connection is queried only -- no rows are created, updated,
deleted, or have their schema changed.

Data sources (all read-only):
- ``reconciliation_review_queue`` (migration 007) -- the queue rows themselves,
  providing ``public_id``, ``candidate_id``, ``issue_type``, ``status``,
  ``priority``, ``reason_codes_json``, ``evidence_json``, ``created_at``, and
  ``run_public_id``.
- ``reconciliation_structured_evidence`` (migration 011) -- counted by
  ``review_queue_public_id`` to derive ``evidence_count``.
- ``reconciliation_resolution_decisions`` (migration 007) and
  ``reconciliation_guarded_apply_operation_results`` (migration 014) -- joined
  to derive ``blocking_count`` (the number of apply operations linked to the
  review item that were blocked, partially blocked, or in conflict).
"""


# Execution statuses that represent a blocking outcome for an apply operation.
# Mirrors the CHECK constraint in migration 014.
_BLOCKING_OPERATION_STATUSES: tuple[str, ...] = ("blocked", "partially_blocked", "conflict")


@dataclass(frozen=True)
class ReconciliationReviewQueueItem:
    """A single read-only preview record for the reconciliation review queue.

    Built by ``summarize_reconciliation_review_queue_from_repository()``.
    Every field is derived deterministically from persisted rows; none are
    random, time-dependent, or AI-generated.  The dataclass is frozen so
    preview records cannot be mutated after construction.
    """

    review_id: str
    source_type: str
    source_id: str
    status: str
    priority: str
    reason: str
    evidence_count: int
    blocking_count: int
    created_at: str | None
    summary: str


def summarize_reconciliation_review_queue_from_repository(
    conn: sqlite3.Connection,
) -> list[ReconciliationReviewQueueItem]:
    """Build a read-only preview of the reconciliation review queue.

    Returns one ``ReconciliationReviewQueueItem`` per persisted review queue
    row, ordered stably by priority (high before medium before low) and then
    by ``review_id`` ascending as a deterministic tie-breaker.

    Only review items that still require human attention are surfaced.  Rows
    whose ``status`` is ``resolved`` or ``ignored`` are excluded; ``pending``
    and ``needs_more_info`` rows are included.  This matches the existing
    project semantics where ``resolved``/``ignored`` items are no longer
    actionable in the review queue.

    The connection is used for SELECT queries only.  No INSERT, UPDATE,
    DELETE, CREATE, DROP, ALTER, or transaction commit is performed.  The
    caller owns the connection lifecycle.
    """
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT
          rq.public_id,
          rq.candidate_id,
          rq.run_public_id,
          rq.issue_type,
          rq.suggested_action,
          rq.priority,
          rq.status,
          rq.reason_codes_json,
          rq.created_at
        FROM reconciliation_review_queue rq
        WHERE rq.status NOT IN ('resolved', 'ignored')
        ORDER BY rq.public_id ASC
        """,
    ).fetchall()

    # Pre-compute evidence counts per review queue public_id in one pass.
    evidence_counts = _load_evidence_counts(conn)

    # Pre-compute blocking operation counts per review queue public_id in one
    # pass.  The chain is:
    #   review_queue.public_id
    #     -> resolution_decisions.review_queue_public_id
    #       (resolution_decisions.public_id == decision_id)
    #     -> guarded_apply_operation_results.decision_id
    #       filtered to blocking execution statuses.
    blocking_counts = _load_blocking_counts(conn)

    items: list[ReconciliationReviewQueueItem] = []
    for row in rows:
        public_id = row["public_id"]
        issue_type = row["issue_type"]
        priority = _priority_label_for_issue(issue_type)
        reason_codes = _decode_reason_codes(row["reason_codes_json"])
        reason = _build_reason(issue_type, reason_codes)
        evidence_count = evidence_counts.get(public_id, 0)
        blocking_count = blocking_counts.get(public_id, 0)
        summary = _build_preview_summary(
            status=row["status"],
            priority=priority,
            issue_type=issue_type,
            evidence_count=evidence_count,
            blocking_count=blocking_count,
        )
        items.append(
            ReconciliationReviewQueueItem(
                review_id=public_id,
                source_type="reconciliation_review_queue",
                source_id=row["candidate_id"],
                status=row["status"],
                priority=priority,
                reason=reason,
                evidence_count=evidence_count,
                blocking_count=blocking_count,
                created_at=row["created_at"],
                summary=summary,
            )
        )

    # Stable, priority-aware ordering: high -> medium -> low, then review_id.
    items.sort(key=lambda item: item.review_id)
    items.sort(key=lambda item: _priority_rank(item.priority))
    return items


# ---------------------------------------------------------------------------
# Preview v1 internal helpers -- read-only and deterministic
# ---------------------------------------------------------------------------


def _load_evidence_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Return a mapping of ``review_queue_public_id`` -> evidence row count.

    Reads ``reconciliation_structured_evidence`` (migration 011) only.
    """
    rows = conn.execute(
        """
        SELECT review_queue_public_id, COUNT(*) AS cnt
        FROM reconciliation_structured_evidence
        WHERE review_queue_public_id IS NOT NULL
        GROUP BY review_queue_public_id
        """,
    ).fetchall()
    return {row["review_queue_public_id"]: int(row["cnt"]) for row in rows}


def _load_blocking_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Return a mapping of ``review_queue_public_id`` -> blocking operation count.

    Joins ``reconciliation_resolution_decisions`` (migration 007) to
    ``reconciliation_guarded_apply_operation_results`` (migration 014) and
    counts operations whose execution status is blocking.  Reads only.
    """
    placeholders = ", ".join("?" for _ in _BLOCKING_OPERATION_STATUSES)
    rows = conn.execute(
        f"""
        SELECT d.review_queue_public_id, COUNT(*) AS cnt
        FROM reconciliation_resolution_decisions d
        JOIN reconciliation_guarded_apply_operation_results op
          ON op.decision_id = d.public_id
        WHERE op.execution_status IN ({placeholders})
        GROUP BY d.review_queue_public_id
        """,
        _BLOCKING_OPERATION_STATUSES,
    ).fetchall()
    return {row["review_queue_public_id"]: int(row["cnt"]) for row in rows}


def _priority_label_for_issue(issue_type: str) -> str:
    """Map a persisted ``issue_type`` string to a stable priority label.

    Falls back to ``low`` for unknown issue types so the preview never breaks
    on forward-compatible new issue types persisted by future migrations.
    """
    try:
        return review_priority_for_issue(IssueType(issue_type)).value
    except ValueError:
        return ReviewPriority.LOW.value


def _priority_rank(priority: str) -> int:
    """Sort HIGH before MEDIUM before LOW for the preview queue."""
    if priority == ReviewPriority.HIGH.value:
        return 0
    if priority == ReviewPriority.MEDIUM.value:
        return 1
    return 2


def _decode_reason_codes(reason_codes_json: str | None) -> list[str]:
    """Decode the persisted ``reason_codes_json`` into a list, tolerating NULL."""
    return json.loads(reason_codes_json or "[]")


def _build_reason(issue_type: str, reason_codes: list[str]) -> str:
    """Build a deterministic reason string from issue type and reason codes."""
    if reason_codes:
        return f"{issue_type} ({', '.join(reason_codes)})"
    return issue_type


def _build_preview_summary(
    *,
    status: str,
    priority: str,
    issue_type: str,
    evidence_count: int,
    blocking_count: int,
) -> str:
    """Build a concise, deterministic human-readable summary string."""
    blocking_clause = f", {blocking_count} blocking" if blocking_count else ""
    return (
        f"[{priority}] {issue_type} item ({status}, "
        f"{evidence_count} evidence{blocking_clause}) pending review"
    )


# Preview v1 extended __all__
__all__ = __all__ + [
    "ReconciliationReviewQueueItem",
    "summarize_reconciliation_review_queue_from_repository",
]
