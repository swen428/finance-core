"""Reconciliation Review Queue v1 -- deterministic batch matching engine.

Takes a list of statement transactions and a list of app transactions,
runs individual match attempts through the existing ``match_statement()``
function, and produces ``ReconciliationCandidate`` objects enriched with
issue types and confidence scores.

Key additions over the foundation matcher:
- ``missing_in_app``: a statement has no matching app transaction.
- ``missing_in_statement``: an app transaction was never referenced by any statement candidate.
- Batch-level duplicate detection across all statement → app pairings.

Design properties:
- Deterministic: same inputs always produce the same output.
- In-memory only: never touches a database.
- Delegates to ``match_statement()`` for per-transaction matching.
- Uses the new ``IssueType`` enum for review queue classification.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from finance_core.reconciliation.matcher import match_statement
from finance_core.reconciliation.models import (
    AppTransaction,
    InternalCandidate,
    IssueType,
    MatchResult,
    MatchStatus,
    ReasonCode,
    ReconciliationCandidate,
    StatementAmountDirection,
    StatementTransaction,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def match_batch(
    statements: Sequence[StatementTransaction],
    app_transactions: Sequence[AppTransaction],
    *,
    date_tolerance_days: int = 3,
    posted_date_window_days: int = 3,
    merchant_similarity_threshold: float = 0.5,
) -> list[ReconciliationCandidate]:
    """Match every statement transaction against the pool of app transactions.

    Parameters
    ----------
    statements:
        Structured statement transactions from a bank/card statement.
    app_transactions:
        App-side transactions used as reconciliation candidates.
    date_tolerance_days:
        Maximum allowed absolute day-delta for date matching.
    merchant_similarity_threshold:
        Minimum Jaccard token-similarity for merchant matching.

    Returns
    -------
    list[ReconciliationCandidate]
        One candidate per statement transaction, each carrying the match
        outcome, issue type, and confidence score.
    """
    # Convert AppTransaction → InternalCandidate for the existing matcher
    internal_candidates = _app_to_internal(app_transactions)

    candidates: list[ReconciliationCandidate] = []
    referenced_app_ids: set[str] = set()

    for i, stmt in enumerate(statements):
        result = match_statement(
            stmt,
            internal_candidates,
            date_tolerance_days=date_tolerance_days,
            posted_date_window_days=posted_date_window_days,
            merchant_similarity_threshold=merchant_similarity_threshold,
        )

        # Find the best AppTransaction from the match result
        best_app: AppTransaction | None = None
        if result.best_candidate is not None:
            for app_txn in app_transactions:
                if app_txn.app_txn_id == result.best_candidate.internal_id:
                    best_app = app_txn
                    break
        elif result.candidates and len(result.candidates) > 0:
            # For ambiguous/duplicate cases, try to find the first candidate
            first_cand = result.candidates[0]
            for app_txn in app_transactions:
                if app_txn.app_txn_id == first_cand.internal_id:
                    best_app = app_txn
                    break

        # Collect all app transactions that matched
        all_apps: list[AppTransaction] = []
        if result.candidates:
            for c in result.candidates:
                for app_txn in app_transactions:
                    if app_txn.app_txn_id == c.internal_id:
                        all_apps.append(app_txn)
                        break

        # Determine issue type from match result (inspects reasons for granularity)
        issue_type = _result_to_issue_type(result)
        confidence = _compute_confidence(result)
        reason_codes = result.reasons

        cand_id = f"cand-{i:03d}-{stmt.statement_row_reference or stmt.merchant_raw}"

        candidate = ReconciliationCandidate(
            statement=stmt,
            best_app_transaction=best_app,
            all_app_transactions=tuple(all_apps),
            match_status=result.status,
            reason_codes=reason_codes,
            issue_type=issue_type,
            confidence_score=confidence,
            evidence=result.evidence,
            candidate_id=cand_id,
        )

        # Track all referenced app transactions (not just matched ones).
        # An app that appeared in any candidate MUST NOT also be emitted
        # as a synthetic MISSING_IN_STATEMENT later.
        if best_app is not None:
            referenced_app_ids.add(best_app.app_txn_id)
        for app_txn in all_apps:
            referenced_app_ids.add(app_txn.app_txn_id)

        candidates.append(candidate)

    candidates = _flag_reused_matched_app_transactions(candidates)

    for app_txn in app_transactions:
        if app_txn.app_txn_id not in referenced_app_ids:
            # Create a synthetic candidate for this unmatched app transaction
            cand_id = f"cand-missing-stmt-{app_txn.app_txn_id}"
            synthetic = ReconciliationCandidate(
                statement=StatementTransaction(
                    transaction_date=app_txn.transaction_date,
                    posted_date=app_txn.posted_date,
                    merchant_raw=app_txn.merchant,
                    merchant_normalized=app_txn.normalized_merchant,
                    amount=app_txn.amount,
                    currency=app_txn.currency,
                    amount_direction=StatementAmountDirection.UNKNOWN,
                    raw_amount=str(app_txn.amount),
                ),
                best_app_transaction=app_txn,
                all_app_transactions=(app_txn,),
                match_status=MatchStatus.NO_MATCH,
                reason_codes=(ReasonCode.NO_CANDIDATE_FOUND,),
                issue_type=IssueType.MISSING_IN_STATEMENT,
                confidence_score=Decimal("0.0"),
                evidence=None,
                candidate_id=cand_id,
            )
            candidates.append(synthetic)

    return candidates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result_to_issue_type(result: MatchResult) -> IssueType:
    """Map a MatchResult to the corresponding IssueType for the review queue.

    Inspects both the top-level status and the deterministic reason codes
    for more granular classification (e.g. date_window_match vs exact_match,
    merchant_variation vs exact match).
    """
    status = result.status
    reasons = set(result.reasons)

    # Map non-matched statuses directly
    base_mapping: dict[MatchStatus, IssueType] = {
        MatchStatus.NO_MATCH: IssueType.MISSING_IN_APP,
        MatchStatus.AMOUNT_MISMATCH: IssueType.AMOUNT_MISMATCH,
        MatchStatus.CURRENCY_MISMATCH: IssueType.CURRENCY_MISMATCH,
        MatchStatus.DATE_MISMATCH: IssueType.DATE_MISMATCH,
        MatchStatus.MERCHANT_MISMATCH: IssueType.MERCHANT_MISMATCH,
        MatchStatus.POSSIBLE_DUPLICATE: IssueType.POSSIBLE_DUPLICATE,
        MatchStatus.AMBIGUOUS: IssueType.NEEDS_REVIEW,
        MatchStatus.NEEDS_REVIEW: IssueType.NEEDS_REVIEW,
    }

    if status != MatchStatus.MATCHED:
        return base_mapping.get(status, IssueType.NEEDS_REVIEW)

    # Granular classification for MATCHED status based on reason codes
    has_date_window = ReasonCode.POSTED_DATE_WINDOW_MATCH in reasons
    has_exact_date = (
        ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE in reasons
        or ReasonCode.POSTED_DATE_WITHIN_TOLERANCE in reasons
    )
    has_exact_merchant = ReasonCode.MERCHANT_EXACT_MATCH in reasons
    has_norm_merchant = ReasonCode.MERCHANT_NORMALIZED_MATCH in reasons
    has_sim_merchant = ReasonCode.MERCHANT_SIMILARITY_MATCH in reasons

    # DATE_WINDOW_MATCH: date is via posted_date_window (not exact),
    # everything else strong. But if merchant is also fuzzy, prefer
    # MERCHANT_VARIATION (two weak signals together).
    if has_date_window and not has_exact_date:
        if has_sim_merchant:
            return IssueType.MERCHANT_VARIATION
        return IssueType.DATE_WINDOW_MATCH

    # MERCHANT_VARIATION: similarity-only match.  Normalised-merchant
    # matches (explicit alias map) with an exact date are safe enough
    # to treat as MATCHED; the alias map is conservative and
    # intentional.  Normalised-merchant + window-only date has two
    # uncertain signals — still a variation worth reviewing.
    if has_sim_merchant:
        return IssueType.MERCHANT_VARIATION
    if has_norm_merchant and not has_exact_merchant:
        if has_exact_date:
            return IssueType.MATCHED
        if has_date_window:
            return IssueType.MERCHANT_VARIATION

    return IssueType.MATCHED


def _status_to_issue_type(status: MatchStatus) -> IssueType:
    """Map a MatchStatus to the corresponding IssueType for the review queue."""
    mapping: dict[MatchStatus, IssueType] = {
        MatchStatus.MATCHED: IssueType.MATCHED,
        MatchStatus.NO_MATCH: IssueType.MISSING_IN_APP,
        MatchStatus.AMOUNT_MISMATCH: IssueType.AMOUNT_MISMATCH,
        MatchStatus.CURRENCY_MISMATCH: IssueType.CURRENCY_MISMATCH,
        MatchStatus.DATE_MISMATCH: IssueType.DATE_MISMATCH,
        MatchStatus.MERCHANT_MISMATCH: IssueType.MERCHANT_MISMATCH,
        MatchStatus.POSSIBLE_DUPLICATE: IssueType.POSSIBLE_DUPLICATE,
        MatchStatus.AMBIGUOUS: IssueType.NEEDS_REVIEW,
        MatchStatus.NEEDS_REVIEW: IssueType.NEEDS_REVIEW,
    }
    return mapping.get(status, IssueType.NEEDS_REVIEW)


def _flag_reused_matched_app_transactions(
    candidates: list[ReconciliationCandidate],
) -> list[ReconciliationCandidate]:
    """Downgrade matched rows when multiple statements claim the same app row."""
    matched_app_counts: dict[str, int] = {}
    for candidate in candidates:
        if (
            candidate.match_status == MatchStatus.MATCHED
            and candidate.best_app_transaction is not None
        ):
            app_id = candidate.best_app_transaction.app_txn_id
            matched_app_counts[app_id] = matched_app_counts.get(app_id, 0) + 1

    reused_app_ids = {app_id for app_id, count in matched_app_counts.items() if count > 1}
    if not reused_app_ids:
        return candidates

    flagged: list[ReconciliationCandidate] = []
    for candidate in candidates:
        best_app = candidate.best_app_transaction
        if (
            candidate.match_status == MatchStatus.MATCHED
            and best_app is not None
            and best_app.app_txn_id in reused_app_ids
        ):
            reasons = candidate.reason_codes
            if ReasonCode.MULTIPLE_CANDIDATE_MATCHES not in reasons:
                reasons = (*reasons, ReasonCode.MULTIPLE_CANDIDATE_MATCHES)
            flagged.append(
                ReconciliationCandidate(
                    statement=candidate.statement,
                    best_app_transaction=best_app,
                    all_app_transactions=candidate.all_app_transactions,
                    match_status=MatchStatus.POSSIBLE_DUPLICATE,
                    reason_codes=reasons,
                    issue_type=IssueType.POSSIBLE_DUPLICATE,
                    confidence_score=Decimal("0.4"),
                    evidence=candidate.evidence,
                    candidate_id=candidate.candidate_id,
                )
            )
        else:
            flagged.append(candidate)
    return flagged


def _compute_confidence(result: MatchResult) -> Decimal:
    """Compute a deterministic confidence score from a match result.

    Matched / date_window / merchant_variation:
        scaled by date exactness and merchant match strength.
    Mismatch / no-match: 0.0.
    Ambiguous: 0.3.
    Possible duplicate: 0.4.
    """
    if result.status == MatchStatus.MATCHED:
        reasons = set(result.reasons)
        # Base confidence from merchant match quality
        if ReasonCode.MERCHANT_EXACT_MATCH in reasons:
            base = Decimal("1.0")
        elif ReasonCode.MERCHANT_NORMALIZED_MATCH in reasons:
            base = Decimal("0.8")
        elif ReasonCode.MERCHANT_SIMILARITY_MATCH in reasons:
            base = Decimal("0.6")
        else:
            base = Decimal("0.7")
        # Penalise date-window matches slightly
        if ReasonCode.POSTED_DATE_WINDOW_MATCH in reasons:
            base = base - Decimal("0.1")
        return max(base, Decimal("0.5"))
    if result.status == MatchStatus.AMBIGUOUS:
        return Decimal("0.3")
    if result.status == MatchStatus.POSSIBLE_DUPLICATE:
        return Decimal("0.4")
    return Decimal("0.0")


def _app_to_internal(app_txns: Sequence[AppTransaction]) -> list[InternalCandidate]:
    """Convert AppTransaction objects to InternalCandidate objects for the
    existing matcher."""
    result: list[InternalCandidate] = []
    for app_txn in app_txns:
        result.append(
            InternalCandidate(
                internal_id=app_txn.app_txn_id,
                transaction_date=app_txn.transaction_date,
                merchant=app_txn.merchant,
                amount=app_txn.amount,
                currency=app_txn.currency,
                source_type=app_txn.source_type,
                source_channel=app_txn.source_channel,
                transaction_type=app_txn.transaction_type,
            )
        )
    return result


def build_summary(candidates: list[ReconciliationCandidate]) -> dict:
    """Build a dictionary summary from a list of reconciliation candidates.

    Returns counts by issue type for use in CLI and test assertions.
    """
    counts: dict[str, int] = {}
    for c in candidates:
        key = c.issue_type.value
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = [
    "match_batch",
    "build_summary",
    "_status_to_issue_type",
    "_result_to_issue_type",
    "_compute_confidence",
]
