"""Reconciliation Review Evidence Bundle v1 -- read-only composition layer that
assembles a deterministic evidence bundle for each review queue item.

The bundle collects all related structured evidence records and packages
them into a ``ReviewEvidenceBundle`` with an ``ExplanationInput`` DTO
suitable for human review, future AI explanation, Metabase/reporting, and
future UI/API display.

Key design properties:
- **Read-only**: never creates, updates, or deletes evidence rows or any
  other database records.
- **Deterministic**: same inputs always produce the same bundle.
- **Deduplication**: evidence records are included at most once even when
  discoverable through multiple keys.
- **Malformed-payload handling**: malformed evidence payloads are retained
  as raw text; parsed_payload is null with a warning.
- **Missing-evidence behavior**: a review queue item with no evidence
  receives an empty evidence list and a warning.

Non-goals (explicitly out of scope):
- Auto-apply, auto-resolution, or automatic transaction correction.
- Final transaction mutation, creation, or deletion.
- Settlement obligation generation.
- Telegram / OpenClaw / OCR / PDF runtime integration.
- AI model calls or AI-as-authority judgments.
- Metabase dashboard implementation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from finance_core.reconciliation.structured_evidence import (
    EvidenceQueryResult,
    EvidenceReader,
)

# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class ReviewEvidenceBundleError(Exception):
    """Base exception for review evidence bundle errors."""


class ReviewQueueItemNotFoundError(ReviewEvidenceBundleError):
    """Raised when a review queue item cannot be found by public_id."""


# ---------------------------------------------------------------------------
# Explanation input DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExplanationInput:
    """Deterministic DTO packaging structured evidence for future explanation.

    This DTO provides enough information for an AI explanation layer or
    human reviewer to understand the review context without mutating any
    underlying records.  It never performs financial calculations, makes
    AI-style judgements, or alters reconciliation state.

    Fields
    ------
    review_queue_public_id:
        The review queue item this explanation is for.
    statement_transaction_id / app_transaction_id:
        Related transaction identifiers, if available from stored evidence.
    match_status:
        Match status from the review queue row, if available.
    amount_delta:
        Amount delta text, if available.
    summary:
        One-paragraph human-readable summary derived from evidence and
        review context.  Deterministic, never AI-generated.
    facts:
        Key-value strings describing facts extracted from evidence and
        review context (e.g. ``"statement_amount: 29.90 SGD"``).
    warnings:
        Non-empty when evidence is missing, malformed, or suspicious.
        Always inspect warnings before treating the bundle as complete.
    evidence_public_ids:
        Public IDs of all evidence records included in the parent bundle,
        in deterministic order.  Useful for traceability and auditing.
    """

    review_queue_public_id: str
    statement_transaction_id: str | None = None
    app_transaction_id: str | None = None
    match_status: str | None = None
    amount_delta: str | None = None
    summary: str = ""
    facts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    evidence_public_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Review evidence bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewEvidenceBundle:
    """Read-only bundle of structured evidence for a single review queue item.

    This is a deterministic snapshot of all evidence records linked to a
    review queue item at collection time.  It is intentionally immutable:
    callers cannot mutate underlying evidence rows through this object.

    Fields
    ------
    review_queue_public_id:
        The review queue item this bundle is for.
    statement_transaction_id / app_transaction_id:
        Related transaction identifiers from stored evidence.
    match_status:
        Match status from the review queue row, if available.
    amount_delta:
        Amount delta text, if available.
    evidence:
        All collected evidence records in deterministic order, with
        duplicates removed.
    explanation_input:
        A pre-built ``ExplanationInput`` with facts, warnings, and
        evidence public IDs for downstream use.
    """

    review_queue_public_id: str
    statement_transaction_id: str | None = None
    app_transaction_id: str | None = None
    match_status: str | None = None
    amount_delta: str | None = None
    evidence: tuple[EvidenceQueryResult, ...] = ()
    explanation_input: ExplanationInput = field(
        default_factory=lambda: ExplanationInput(review_queue_public_id="")
    )


# ---------------------------------------------------------------------------
# Review evidence service -- read-only builder
# ---------------------------------------------------------------------------


class ReviewEvidenceService:
    """Read-only service that builds ``ReviewEvidenceBundle`` objects for
    review queue items.

    Wraps an ``EvidenceReader`` for typed evidence queries and a
    ``sqlite3.Connection`` for review queue row lookup.  This service is
    intentionally read-only and audit-safe:

    - Never creates, updates, or deletes evidence rows.
    - Never mutates review decisions, final transactions, or settlement
      obligations.
    - Never infers financial truth beyond stored evidence and review facts.
    - Never calls an AI model or performs final settlement calculations.

    Usage::

        conn = connect_sqlite(":memory:")
        conn.row_factory = sqlite3.Row
        persistence = StructuredEvidencePersistence(conn)
        reader = EvidenceReader(persistence)
        service = ReviewEvidenceService(conn=conn, evidence_reader=reader)
        bundle = service.build_bundle("rq-000042")
        if bundle is not None:
            print(bundle.explanation_input.summary)
    """

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        evidence_reader: EvidenceReader,
    ) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._reader = evidence_reader

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_bundle(self, review_queue_public_id: str) -> ReviewEvidenceBundle | None:
        """Build a review evidence bundle for a single review queue item.

        Returns ``None`` when no review queue item matches the given
        ``review_queue_public_id``.

        Otherwise returns a frozen ``ReviewEvidenceBundle`` containing:
        - All related structured evidence records (deduplicated).
        - An ``ExplanationInput`` with facts, warnings, and evidence IDs.
        """
        row = self._find_review_queue_row(review_queue_public_id)
        if row is None:
            return None

        # Extract review context from the row
        match_status = row["match_status"] if "match_status" in row.keys() else None
        amount_delta = row["amount_delta"] if "amount_delta" in row.keys() else None
        statement_ref = (
            row["statement_transaction_ref"] if "statement_transaction_ref" in row.keys() else None
        )
        app_ref = row["app_transaction_ref"] if "app_transaction_ref" in row.keys() else None

        # Collect evidence via all available keys (deduplicated by public_id)
        evidence_records, collection_warnings = self._collect_evidence(
            review_queue_public_id=review_queue_public_id,
            statement_transaction_id=statement_ref,
            app_transaction_id=app_ref,
        )

        # Build explanation input
        explanation = self._build_explanation_input(
            review_queue_public_id=review_queue_public_id,
            statement_transaction_id=statement_ref,
            app_transaction_id=app_ref,
            match_status=match_status,
            amount_delta=amount_delta,
            evidence_records=evidence_records,
            collection_warnings=collection_warnings,
        )

        return ReviewEvidenceBundle(
            review_queue_public_id=review_queue_public_id,
            statement_transaction_id=statement_ref,
            app_transaction_id=app_ref,
            match_status=match_status,
            amount_delta=amount_delta,
            evidence=evidence_records,
            explanation_input=explanation,
        )

    # ------------------------------------------------------------------
    # Internal -- review queue lookup
    # ------------------------------------------------------------------

    def _find_review_queue_row(self, public_id: str) -> sqlite3.Row | None:
        """Find a review queue row by public_id.

        Returns ``None`` when no match exists.  This is intentionally a
        simple query -- the review queue persistence layer is the
        canonical owner of review queue rows, but we query directly here
        to avoid a dependency cycle.
        """
        return self._conn.execute(
            """
            SELECT * FROM reconciliation_review_queue
            WHERE public_id = ?
            """,
            (public_id,),
        ).fetchone()

    # ------------------------------------------------------------------
    # Internal -- evidence collection with deduplication
    # ------------------------------------------------------------------

    def _collect_evidence(
        self,
        *,
        review_queue_public_id: str,
        statement_transaction_id: str | None,
        app_transaction_id: str | None,
    ) -> tuple[tuple[EvidenceQueryResult, ...], list[str]]:
        """Collect all evidence records reachable via any available key.

        Deduplication is by ``public_id``.  Ordering follows the
        global stable ordering (``created_at ASC, id ASC``) across all
        collection paths.  Records encountered first are kept; later
        ``public_id`` are skipped.

        Returns a tuple of ``(deduplicated_records, warnings)``.
        """
        seen: set[str] = set()
        collected: list[EvidenceQueryResult] = []
        warnings: list[str] = []

        # Collect by review_queue_public_id
        by_rq = self._reader.list_by_review_queue(review_queue_public_id)
        self._add_unique(by_rq, seen, collected)

        # Collect by statement_transaction_id
        if statement_transaction_id:
            by_stmt = self._reader.list_by_statement_transaction(statement_transaction_id)
            self._add_unique(by_stmt, seen, collected)

        # Collect by app_transaction_id
        if app_transaction_id:
            by_app = self._reader.list_by_app_transaction(app_transaction_id)
            self._add_unique(by_app, seen, collected)

        # Global sort: evidence discovered through different collection
        # paths is ordered by created_at ASC, id ASC (not path order).
        # created_at is ISO-8601 text from SQLite CURRENT_TIMESTAMP;
        # lexical sort on ISO timestamps is equivalent to chronological.
        collected.sort(key=lambda r: (r.created_at, r.id))

        if not collected:
            warnings.append("no_structured_evidence_found")

        return tuple(collected), warnings

    @staticmethod
    def _add_unique(
        records: list[EvidenceQueryResult],
        seen: set[str],
        collected: list[EvidenceQueryResult],
    ) -> None:
        """Add records whose public_id has not been seen yet."""
        for r in records:
            if r.public_id not in seen:
                seen.add(r.public_id)
                collected.append(r)

    # ------------------------------------------------------------------
    # Internal -- explanation input builder
    # ------------------------------------------------------------------

    def _build_explanation_input(
        self,
        *,
        review_queue_public_id: str,
        statement_transaction_id: str | None,
        app_transaction_id: str | None,
        match_status: str | None,
        amount_delta: str | None,
        evidence_records: tuple[EvidenceQueryResult, ...],
        collection_warnings: list[str],
    ) -> ExplanationInput:
        """Build a deterministic ``ExplanationInput`` from collected evidence
        and review context.
        """
        facts: list[str] = []
        warnings = list(collection_warnings)  # mutable copy

        # Evidence public IDs
        evidence_public_ids = tuple(r.public_id for r in evidence_records)

        # Build facts from evidence
        evidence_type_counts: dict[str, int] = {}
        malformed_count = 0

        for r in evidence_records:
            # Count evidence types
            evidence_type_counts[r.evidence_type] = evidence_type_counts.get(r.evidence_type, 0) + 1

            # Check for malformed payload
            parsed = r.parsed_payload_or_none()
            if parsed is None:
                malformed_count += 1
                warnings.append(f"malformed_evidence_payload: public_id={r.public_id}")

        # Summary sentence
        total = len(evidence_records)
        if total == 0:
            summary = (
                f"No structured evidence records found for review queue item "
                f"{review_queue_public_id}."
            )
        else:
            type_summary = ", ".join(
                f"{count} {etype}" for etype, count in sorted(evidence_type_counts.items())
            )
            summary = (
                f"Review queue item {review_queue_public_id} has "
                f"{total} structured evidence record"
                f"{'s' if total != 1 else ''}"
            )
            if type_summary:
                summary += f" ({type_summary})"
            summary += "."

        # Facts from evidence counts
        for etype, count in sorted(evidence_type_counts.items()):
            facts.append(f"evidence_type_{etype}: {count} record(s)")

        # Context facts
        if match_status is not None:
            facts.append(f"match_status: {match_status}")
        if amount_delta is not None:
            facts.append(f"amount_delta: {amount_delta}")
        if statement_transaction_id is not None:
            facts.append(f"statement_transaction_id: {statement_transaction_id}")
        if app_transaction_id is not None:
            facts.append(f"app_transaction_id: {app_transaction_id}")
        facts.append(f"total_evidence_records: {total}")
        if malformed_count > 0:
            facts.append(f"malformed_evidence_payloads: {malformed_count}")

        # Warnings for missing context
        if statement_transaction_id is None:
            warnings.append("missing_statement_transaction_id")
        if app_transaction_id is None:
            warnings.append("missing_app_transaction_id")

        # Check for conflicting evidence types
        if (
            "matching_decision" in evidence_type_counts
            and "review_resolution" in evidence_type_counts
        ):
            warnings.append("conflicting_evidence_types: matching_decision + review_resolution")

        return ExplanationInput(
            review_queue_public_id=review_queue_public_id,
            statement_transaction_id=statement_transaction_id,
            app_transaction_id=app_transaction_id,
            match_status=match_status,
            amount_delta=amount_delta,
            summary=summary,
            facts=tuple(facts),
            warnings=tuple(warnings),
            evidence_public_ids=evidence_public_ids,
        )


__all__ = [
    "ExplanationInput",
    "ReviewEvidenceBundle",
    "ReviewEvidenceBundleError",
    "ReviewEvidenceService",
    "ReviewQueueItemNotFoundError",
]
