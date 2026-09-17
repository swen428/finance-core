"""Reconciliation Foundation v1 -- deterministic matcher.

The matcher accepts a single StatementTransaction and a sequence of
InternalCandidate records and returns a MatchResult with a status and
deterministic reason codes.

Key design properties:
- Deterministic: same inputs always produce the same result.
- Stepwise evaluation: amount, currency, date, merchant, then disambiguation.
- Amount and currency are strict gates; date tolerance is configurable.
- Merchant matching is conservative and never overrides amount/currency/date failures.
- Multiple candidates trigger ambiguous/duplicate handling, not silent picking.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Sequence

from finance_core.reconciliation.decision import (
    COMPATIBILITY_VERSION,
    MATCHER_VERSION,
    MERCHANT_NORMALIZATION_VERSION,
    CandidateEvaluation,
    CompatibilityResult,
    build_decision_record,
    evaluate_candidate,
    original_amount_sign,
    statement_precheck,
)
from finance_core.reconciliation.models import (
    InternalCandidate,
    MatchEvidence,
    MatchResult,
    MatchStatus,
    ReasonCode,
    StatementTransaction,
    normalize_merchant,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def match_statement(
    statement: StatementTransaction,
    candidates: Sequence[InternalCandidate],
    *,
    date_tolerance_days: int = 3,
    posted_date_window_days: int = 3,
    merchant_similarity_threshold: float = 0.5,
    matcher_version: str = MATCHER_VERSION,
    compatibility_version: str = COMPATIBILITY_VERSION,
    merchant_normalization_version: str = MERCHANT_NORMALIZATION_VERSION,
    authorization_public_id: str | None = None,
) -> MatchResult:
    """Match one statement transaction against a pool of internal candidates.

    Parameters
    ----------
    statement:
        A structured statement transaction from an imported bank / credit-card
        statement.
    candidates:
        Internal Finance records to match against.  May be empty.
    date_tolerance_days:
        Maximum allowed absolute day-delta between statement and candidate
        transaction dates for a date match.  Defaults to 3 days.
    merchant_similarity_threshold:
        Minimum Jaccard token-similarity score for a candidate's merchant to
        be considered a *potential* match (0.0--1.0).  Defaults to 0.5.

    Returns
    -------
    MatchResult
        Deterministic match outcome with status, reasons, evidence, and
        the best candidate when matched.
    """
    _validate_matcher_parameters(
        date_tolerance_days,
        posted_date_window_days,
        merchant_similarity_threshold,
    )
    if len({candidate.internal_id for candidate in candidates}) != len(candidates):
        raise ValueError("Candidate identities must be unique")

    evaluations = tuple(
        evaluate_candidate(
            statement,
            candidate,
            date_tolerance_days=date_tolerance_days,
            posted_date_window_days=posted_date_window_days,
            merchant_similarity_threshold=merchant_similarity_threshold,
        )
        for candidate in candidates
    )

    precheck = statement_precheck(statement)
    if precheck is not None:
        return attach_decision_hash(
            MatchResult(
                status=MatchStatus.NEEDS_REVIEW,
                reasons=(precheck,),
                evidence=_baseline_evidence(statement, candidates),
                statement=statement,
                candidates=tuple(candidates),
            ),
            statement,
            evaluations,
            date_tolerance_days=date_tolerance_days,
            posted_date_window_days=posted_date_window_days,
            merchant_similarity_threshold=merchant_similarity_threshold,
            matcher_version=matcher_version,
            compatibility_version=compatibility_version,
            merchant_normalization_version=merchant_normalization_version,
            authorization_public_id=authorization_public_id,
        )

    if statement.transaction_date is None and statement.posted_date is None:
        result = MatchResult(
            status=MatchStatus.NEEDS_REVIEW,
            reasons=(ReasonCode.MISSING_STATEMENT_DATE,),
            evidence=_baseline_evidence(statement, candidates),
            statement=statement,
            candidates=tuple(candidates),
        )
    elif not candidates:
        result = MatchResult(
            status=MatchStatus.NO_MATCH,
            reasons=(ReasonCode.NO_CANDIDATE_FOUND,),
            evidence=_baseline_evidence(statement, candidates),
            statement=statement,
        )
    else:
        result = _result_from_evaluations(statement, evaluations, date_tolerance_days)

    return attach_decision_hash(
        result,
        statement,
        evaluations,
        date_tolerance_days=date_tolerance_days,
        posted_date_window_days=posted_date_window_days,
        merchant_similarity_threshold=merchant_similarity_threshold,
        matcher_version=matcher_version,
        compatibility_version=compatibility_version,
        merchant_normalization_version=merchant_normalization_version,
        authorization_public_id=authorization_public_id,
    )


def attach_decision_hash(
    result: MatchResult,
    statement: StatementTransaction,
    evaluations: Sequence[CandidateEvaluation],
    *,
    date_tolerance_days: int = 3,
    posted_date_window_days: int = 3,
    merchant_similarity_threshold: float = 0.5,
    matcher_version: str = MATCHER_VERSION,
    compatibility_version: str = COMPATIBILITY_VERSION,
    merchant_normalization_version: str = MERCHANT_NORMALIZATION_VERSION,
    authorization_public_id: str | None = None,
) -> MatchResult:
    record = build_decision_record(
        statement,
        result,
        evaluations,
        date_tolerance_days=date_tolerance_days,
        posted_date_window_days=posted_date_window_days,
        merchant_similarity_threshold=merchant_similarity_threshold,
        matcher_version=matcher_version,
        compatibility_version=compatibility_version,
        merchant_normalization_version=merchant_normalization_version,
        authorization_public_id=authorization_public_id,
    )
    return replace(
        result,
        decision_hash=record.decision_hash,
        decision_material_json=record.decision_material_json,
        candidate_set_fingerprint=record.candidate_set_fingerprint,
        decision_contract_version="reconciliation-decision-v1",
        matcher_version=matcher_version,
        compatibility_version=compatibility_version,
        merchant_normalization_version=merchant_normalization_version,
        authorization_public_id=authorization_public_id,
    )


def _result_from_evaluations(
    statement: StatementTransaction,
    evaluations: Sequence[CandidateEvaluation],
    date_tolerance_days: int,
) -> MatchResult:
    currency_matched = [
        evaluation
        for evaluation in evaluations
        if "currency_compatible" in evaluation.hard_gate_results
    ]
    if not currency_matched:
        return MatchResult(
            status=MatchStatus.CURRENCY_MISMATCH,
            reasons=(ReasonCode.CURRENCY_DIFFERS,),
            evidence=_baseline_evidence(statement, [e.candidate for e in evaluations]),
            statement=statement,
            candidates=tuple(e.candidate for e in evaluations),
        )

    direction_matched = [
        evaluation
        for evaluation in currency_matched
        if evaluation.compatibility is CompatibilityResult.COMPATIBLE
    ]
    if not direction_matched:
        review = next(
            (
                evaluation
                for evaluation in currency_matched
                if evaluation.compatibility is CompatibilityResult.REVIEW_REQUIRED
            ),
            currency_matched[0],
        )
        return MatchResult(
            status=MatchStatus.NEEDS_REVIEW,
            reasons=(review.compatibility_reason,),
            evidence=_evaluation_evidence(statement, review, date_tolerance_days),
            statement=statement,
            candidates=tuple(e.candidate for e in currency_matched),
        )

    amount_matched = [
        evaluation
        for evaluation in direction_matched
        if ReasonCode.EXACT_AMOUNT_MATCH.value in evaluation.hard_gate_results
    ]
    if not amount_matched:
        evaluation = direction_matched[0]
        return MatchResult(
            status=MatchStatus.AMOUNT_MISMATCH,
            reasons=(ReasonCode.AMOUNT_DIFFERS,),
            evidence=_evaluation_evidence(statement, evaluation, date_tolerance_days),
            statement=statement,
            candidates=tuple(e.candidate for e in direction_matched),
        )

    date_matched = [
        evaluation
        for evaluation in amount_matched
        if evaluation.date_result is not None and evaluation.date_result.matched
    ]
    if not date_matched:
        evaluation = amount_matched[0]
        return MatchResult(
            status=MatchStatus.DATE_MISMATCH,
            reasons=(ReasonCode.OUTSIDE_DATE_TOLERANCE,),
            evidence=_evaluation_evidence(statement, evaluation, date_tolerance_days),
            statement=statement,
            candidates=tuple(e.candidate for e in amount_matched),
        )

    scored = [evaluation for evaluation in date_matched if evaluation.eligible]
    if not scored:
        evaluation = date_matched[0]
        return MatchResult(
            status=MatchStatus.MERCHANT_MISMATCH,
            reasons=(ReasonCode.WEAK_MERCHANT_MATCH,),
            evidence=_evaluation_evidence(statement, evaluation, date_tolerance_days),
            statement=statement,
            candidates=tuple(e.candidate for e in date_matched),
        )

    scored.sort(
        key=lambda evaluation: (
            -(evaluation.merchant_similarity_score or Decimal("0")),
            evaluation.candidate.internal_id,
        )
    )
    if len(scored) > 1:
        first, second = scored[:2]
        status = (
            MatchStatus.POSSIBLE_DUPLICATE
            if _candidates_are_duplicates(first.candidate, second.candidate)
            else MatchStatus.AMBIGUOUS
        )
        return MatchResult(
            status=status,
            reasons=(ReasonCode.MULTIPLE_CANDIDATE_MATCHES,),
            evidence=_evaluation_evidence(statement, first, date_tolerance_days),
            statement=statement,
            candidates=tuple(e.candidate for e in scored),
        )

    evaluation = scored[0]
    date_result = evaluation.date_result
    assert date_result is not None
    reasons: list[ReasonCode] = [
        ReasonCode.SAME_CURRENCY,
        ReasonCode.DIRECTION_TYPE_COMPATIBLE,
        ReasonCode.EXACT_AMOUNT_MATCH,
    ]
    if date_result.matched_on == "transaction_date":
        reasons.append(ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE)
    elif date_result.matched_on == "posted_date":
        reasons.append(ReasonCode.POSTED_DATE_WITHIN_TOLERANCE)
    elif date_result.matched_on == "posted_date_window":
        reasons.append(ReasonCode.POSTED_DATE_WINDOW_MATCH)
    if evaluation.merchant_exact:
        reasons.append(ReasonCode.MERCHANT_EXACT_MATCH)
    elif evaluation.merchant_normalized_match:
        reasons.append(ReasonCode.MERCHANT_NORMALIZED_MATCH)
    else:
        reasons.append(ReasonCode.MERCHANT_SIMILARITY_MATCH)
    return MatchResult(
        status=MatchStatus.MATCHED,
        reasons=tuple(reasons),
        evidence=_evaluation_evidence(statement, evaluation, date_tolerance_days),
        statement=statement,
        best_candidate=evaluation.candidate,
        candidates=(evaluation.candidate,),
    )


def _baseline_evidence(
    statement: StatementTransaction,
    candidates: Sequence[InternalCandidate],
) -> MatchEvidence:
    return MatchEvidence(
        statement_amount=statement.amount,
        statement_currency=statement.currency,
        statement_txn_date=statement.transaction_date,
        statement_posted_date=statement.posted_date,
        statement_merchant=statement.merchant_raw,
        statement_merchant_normalized=statement.merchant_normalized
        or normalize_merchant(statement.merchant_raw),
        candidate_count=len(candidates),
        statement_direction=statement.amount_direction.value
        if statement.amount_direction is not None
        else None,
        original_amount_text=statement.raw_amount,
        original_amount_sign=original_amount_sign(statement).value,
    )


def _evaluation_evidence(
    statement: StatementTransaction,
    evaluation: CandidateEvaluation,
    date_tolerance_days: int,
) -> MatchEvidence:
    date_result = evaluation.date_result
    return MatchEvidence(
        statement_amount=statement.amount,
        candidate_amount=evaluation.candidate.amount,
        statement_currency=statement.currency,
        candidate_currency=evaluation.candidate.currency,
        statement_txn_date=statement.transaction_date,
        statement_posted_date=statement.posted_date,
        candidate_txn_date=evaluation.candidate.transaction_date,
        date_delta_days=date_result.day_delta if date_result is not None else None,
        date_tolerance_days=date_tolerance_days,
        statement_merchant=statement.merchant_raw,
        candidate_merchant=evaluation.candidate.merchant,
        statement_merchant_normalized=evaluation.statement_merchant_normalized,
        candidate_merchant_normalized=evaluation.candidate_merchant_normalized,
        merchant_similarity=float(evaluation.merchant_similarity_score)
        if evaluation.merchant_similarity_score is not None
        else None,
        candidate_count=1,
        statement_direction=statement.amount_direction.value
        if statement.amount_direction is not None
        else None,
        candidate_transaction_type=evaluation.transaction_type.value,
        original_amount_text=statement.raw_amount,
        original_amount_sign=original_amount_sign(statement).value,
        compatibility_result=evaluation.compatibility.value,
        hard_gate_results=evaluation.hard_gate_results,
    )


def _validate_matcher_parameters(
    date_tolerance_days: int,
    posted_date_window_days: int,
    merchant_similarity_threshold: float,
) -> None:
    if date_tolerance_days < 0 or posted_date_window_days < 0:
        raise ValueError("Date tolerance values must be non-negative")
    if not 0 <= merchant_similarity_threshold <= 1:
        raise ValueError("merchant_similarity_threshold must be between 0 and 1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candidates_are_duplicates(a: InternalCandidate, b: InternalCandidate) -> bool:
    """Two candidates are duplicates only when they match on ALL core dimensions:
    amount, currency, transaction_date, AND merchant (normalized).
    """
    return (
        a.amount == b.amount
        and a.currency == b.currency
        and a.transaction_date == b.transaction_date
        and normalize_merchant(a.merchant) == normalize_merchant(b.merchant)
    )
