"""Reconciliation Service Runtime v1 -- orchestrates a reconciliation run
from SQLite-backed statement transactions through the deterministic matcher.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Sequence

from finance_core.financial_audit import (
    AuditEventCommand,
    append_financial_audit_event,
    derive_audit_event_public_id,
)
from finance_core.reconciliation.decision import MATCHER_VERSION, evaluate_candidate
from finance_core.reconciliation.matcher import attach_decision_hash, match_statement
from finance_core.reconciliation.models import (
    InternalCandidate,
    MatchResult,
    MatchStatus,
    ReasonCode,
    StatementAmountDirection,
    StatementTransaction,
)
from finance_core.reconciliation.repository import ReconciliationRepository

# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationRunSummary:
    """Deterministic summary of a completed reconciliation run."""

    run_id: int
    run_public_id: str
    batch_id: int | None
    total_statement_transactions: int
    matched_count: int = 0
    no_match_count: int = 0
    amount_mismatch_count: int = 0
    currency_mismatch_count: int = 0
    date_mismatch_count: int = 0
    merchant_mismatch_count: int = 0
    possible_duplicate_count: int = 0
    ambiguous_count: int = 0
    needs_review_count: int = 0
    failed: bool = False
    match_status_counts: dict[str, int] = field(default_factory=dict)


class ReconciliationTransactionError(RuntimeError):
    """The service cannot safely own its required write transaction."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ReconciliationService:
    """Orchestrates reconciliation runs over a ReconciliationRepository."""

    def __init__(self, repository: ReconciliationRepository) -> None:
        self._repo = repository

    def run_reconciliation_for_batch(
        self,
        batch_id: int,
        candidates: Sequence[InternalCandidate],
        run_public_id: str,
        *,
        matcher_version: str | None = None,
    ) -> ReconciliationRunSummary:
        """Execute a full reconciliation run for one import batch.

        Parameters
        ----------
        batch_id:
            The import batch whose statement_transactions to reconcile.
        candidates:
            In-memory InternalCandidate records to match against.
        run_public_id:
            Unique public_id for the new reconciliation run.
        matcher_version:
            Optional version label persisted on the run record.

        Returns
        -------
        ReconciliationRunSummary with deterministic status counts.
        """
        conn = self._repo.connection
        if conn.in_transaction:
            raise ReconciliationTransactionError(
                "Reconciliation requires a clean caller connection for BEGIN IMMEDIATE"
            )
        effective_matcher_version = matcher_version or MATCHER_VERSION
        run_id: int | None = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            run_id = self._repo.create_reconciliation_run(
                public_id=run_public_id,
                batch_id=batch_id,
                matcher_version=effective_matcher_version,
            )

            rows = self._repo.get_statement_transactions_by_batch(batch_id)

            counts: dict[str, int] = {
                "matched": 0,
                "no_match": 0,
                "amount_mismatch": 0,
                "currency_mismatch": 0,
                "date_mismatch": 0,
                "merchant_mismatch": 0,
                "possible_duplicate": 0,
                "ambiguous": 0,
                "needs_review": 0,
            }

            matched_rows: list[tuple[sqlite3.Row, MatchResult]] = []
            for row in rows:
                stmt = _row_to_statement_transaction(row)
                result = match_statement(
                    stmt,
                    candidates,
                    matcher_version=effective_matcher_version,
                )
                matched_rows.append((row, result))

            matched_rows = _flag_reused_matched_candidates(
                matched_rows,
                candidates,
                effective_matcher_version,
            )

            for row, result in matched_rows:
                match_public_id = f"match-{run_public_id}-{row['public_id']}"
                self._repo.save_match_result(
                    run_id=run_id,
                    statement_transaction_id=row["id"],
                    match_result=result,
                    public_id=match_public_id,
                )
                _append_reconciliation_decision_audit(
                    conn,
                    run_public_id=run_public_id,
                    match_public_id=match_public_id,
                    statement_row=row,
                    result=result,
                )
                self._repo.verify_match_result_integrity(
                    match_public_id,
                    require_audit=True,
                )
                status_key = result.status.value
                if status_key in counts:
                    counts[status_key] += 1

            self._repo.complete_reconciliation_run(run_id)
            conn.commit()

            needs_review_count = sum(v for k, v in counts.items() if k != "matched")

            return ReconciliationRunSummary(
                run_id=run_id,
                run_public_id=run_public_id,
                batch_id=batch_id,
                total_statement_transactions=len(rows),
                matched_count=counts["matched"],
                no_match_count=counts["no_match"],
                amount_mismatch_count=counts["amount_mismatch"],
                currency_mismatch_count=counts["currency_mismatch"],
                date_mismatch_count=counts["date_mismatch"],
                merchant_mismatch_count=counts["merchant_mismatch"],
                possible_duplicate_count=counts["possible_duplicate"],
                ambiguous_count=counts["ambiguous"],
                needs_review_count=needs_review_count,
                match_status_counts=counts,
            )
        except Exception as exc:
            conn.rollback()
            if run_id is not None:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    failed_run_id = self._repo.create_reconciliation_run(
                        public_id=run_public_id,
                        batch_id=batch_id,
                        matcher_version=effective_matcher_version,
                    )
                    self._repo.fail_reconciliation_run(failed_run_id)
                    conn.commit()
                except Exception as fail_exc:
                    conn.rollback()
                    exc.add_note(
                        f"Failed to mark reconciliation run {run_id} as failed: {fail_exc}"
                    )
            raise


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str | None) -> date | None:
    """Parse an ISO date string or return None."""
    if value is None:
        return None
    return date.fromisoformat(value)


def _flag_reused_matched_candidates(
    matched_rows: list[tuple[sqlite3.Row, MatchResult]],
    candidates: Sequence[InternalCandidate],
    matcher_version: str,
) -> list[tuple[sqlite3.Row, MatchResult]]:
    """Downgrade matched rows when several statements claim one candidate."""
    matched_candidate_counts: dict[str, int] = {}
    for _row, result in matched_rows:
        if result.status == MatchStatus.MATCHED and result.best_candidate is not None:
            candidate_id = result.best_candidate.internal_id
            matched_candidate_counts[candidate_id] = (
                matched_candidate_counts.get(candidate_id, 0) + 1
            )

    reused_candidate_ids = {
        candidate_id for candidate_id, count in matched_candidate_counts.items() if count > 1
    }
    if not reused_candidate_ids:
        return matched_rows

    flagged: list[tuple[sqlite3.Row, MatchResult]] = []
    for row, result in matched_rows:
        best_candidate = result.best_candidate
        if (
            result.status == MatchStatus.MATCHED
            and best_candidate is not None
            and best_candidate.internal_id in reused_candidate_ids
        ):
            reasons = result.reasons
            if ReasonCode.MULTIPLE_CANDIDATE_MATCHES not in reasons:
                reasons = (*reasons, ReasonCode.MULTIPLE_CANDIDATE_MATCHES)
            result = MatchResult(
                status=MatchStatus.POSSIBLE_DUPLICATE,
                reasons=reasons,
                evidence=result.evidence,
                statement=result.statement,
                best_candidate=None,
                candidates=result.candidates,
            )
            assert result.statement is not None
            evaluations = tuple(
                evaluate_candidate(
                    result.statement,
                    candidate,
                    date_tolerance_days=3,
                    posted_date_window_days=3,
                    merchant_similarity_threshold=0.5,
                )
                for candidate in candidates
            )
            result = attach_decision_hash(
                result,
                result.statement,
                evaluations,
                matcher_version=matcher_version,
            )
        flagged.append((row, result))

    return flagged


def _parse_decimal(value: float | str | None) -> Decimal | None:
    """Convert a value to Decimal or return None."""
    if value is None:
        return None
    return Decimal(str(value))


def _row_to_statement_transaction(row: sqlite3.Row) -> StatementTransaction:
    """Build a StatementTransaction domain model from a DB row."""
    return StatementTransaction(
        transaction_date=_parse_date(row["transaction_date"]),
        posted_date=_parse_date(row["posted_date"]),
        merchant_raw=row["merchant_raw"],
        merchant_normalized=row["merchant_normalized"]
        if "merchant_normalized" in row.keys()
        else None,
        amount=_parse_decimal(row["amount"]),
        currency=row["currency"],
        account_name=row["account_name"] if "account_name" in row.keys() else None,
        account_id=str(row["account_id"]) if row["account_id"] is not None else None,
        source_batch_id=str(row["batch_id"]) if "batch_id" in row.keys() else None,
        statement_row_reference=row["statement_row_reference"]
        if "statement_row_reference" in row.keys()
        else None,
        public_id=row["public_id"],
        row_fingerprint=row["row_fingerprint"] if "row_fingerprint" in row.keys() else None,
        source_content_hash=row["source_content_hash"]
        if "source_content_hash" in row.keys()
        else None,
        amount_direction=StatementAmountDirection(row["amount_direction"])
        if "amount_direction" in row.keys() and row["amount_direction"] is not None
        else None,
        raw_amount=row["raw_amount"] if "raw_amount" in row.keys() else None,
        raw_amount_type=row["raw_amount_type"] if "raw_amount_type" in row.keys() else None,
    )


def _append_reconciliation_decision_audit(
    conn: sqlite3.Connection,
    *,
    run_public_id: str,
    match_public_id: str,
    statement_row: sqlite3.Row,
    result: MatchResult,
) -> None:
    if result.decision_hash is None:
        raise RuntimeError("A durable reconciliation decision requires a verified decision hash")
    event_type = "reconciliation_decision_recorded"
    event_public_id = derive_audit_event_public_id(
        aggregate_type="reconciliation_match_result",
        aggregate_public_id=match_public_id,
        event_type=event_type,
        causation_public_id=result.decision_hash,
    )
    evidence_refs = [f"statement-row:{statement_row['public_id']}"]
    if "row_fingerprint" in statement_row.keys() and statement_row["row_fingerprint"]:
        evidence_refs.append(f"row-fingerprint:{statement_row['row_fingerprint']}")
    if "source_content_hash" in statement_row.keys() and statement_row["source_content_hash"]:
        evidence_refs.append(f"source-content-sha256:{statement_row['source_content_hash']}")
    state = {
        "decision_hash": result.decision_hash,
        "candidate_set_fingerprint": result.candidate_set_fingerprint,
        "match_status": result.status.value,
        "reason_codes": [reason.value for reason in result.reasons],
        "authorization_public_id": result.authorization_public_id,
    }
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_public_id,
            aggregate_type="reconciliation_match_result",
            aggregate_public_id=match_public_id,
            event_type=event_type,
            event_payload=state,
            previous_state=None,
            new_state=state,
            actor_type="system",
            actor_public_id="reconciliation-matcher",
            authorization_public_id=result.authorization_public_id,
            source_evidence_references=tuple(evidence_refs),
            correlation_public_id=run_public_id,
            causation_public_id=result.decision_hash,
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
    )


__all__ = [
    "ReconciliationService",
    "ReconciliationRunSummary",
    "ReconciliationTransactionError",
]
