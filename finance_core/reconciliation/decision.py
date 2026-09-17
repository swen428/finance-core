"""Versioned reconciliation direction compatibility and decision hashing.

The contract in this module is deterministic and fail-closed.  Currency and
direction/type compatibility are hard gates before amount, date, or merchant
similarity can contribute to a reconciliation decision.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Sequence

from finance_core.calculation.authoritative_snapshot import canonical_json_text
from finance_core.money import canonical_decimal_str
from finance_core.reconciliation.models import (
    DateMatchResult,
    InternalCandidate,
    MatchResult,
    MatchStatus,
    ReasonCode,
    ReconciliationTransactionType,
    StatementAmountDirection,
    StatementTransaction,
    amount_exact_match,
    currency_match,
    match_date_window,
    merchant_similarity,
    normalize_merchant,
)

DECISION_CONTRACT_VERSION = "reconciliation-decision-v1"
MATCHER_VERSION = "reconciliation-matcher-v2"
COMPATIBILITY_VERSION = "reconciliation-direction-compatibility-v1"
MERCHANT_NORMALIZATION_VERSION = "merchant-normalization-v1"
CANDIDATE_SET_VERSION = "reconciliation-candidate-set-v1"

_DECISION_HASH_DOMAIN = "finance-reconciliation-decision-v1"
_CANDIDATE_HASH_DOMAIN = "finance-reconciliation-candidate-set-v1"
_STATEMENT_ID_DOMAIN = "finance-reconciliation-statement-identity-v1"


class CompatibilityResult(str, Enum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    REVIEW_REQUIRED = "review_required"


class OriginalAmountSign(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    ZERO = "zero"
    MISSING = "missing"


@dataclass(frozen=True)
class DirectionCompatibility:
    result: CompatibilityResult
    reason_code: ReasonCode
    transaction_type: ReconciliationTransactionType


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate: InternalCandidate
    transaction_type: ReconciliationTransactionType
    compatibility: CompatibilityResult
    compatibility_reason: ReasonCode
    hard_gate_results: tuple[str, ...]
    date_result: DateMatchResult | None = None
    statement_merchant_normalized: str | None = None
    candidate_merchant_normalized: str | None = None
    merchant_similarity_score: Decimal | None = None
    merchant_exact: bool = False
    merchant_normalized_match: bool = False
    eligible: bool = False


@dataclass(frozen=True)
class DecisionRecord:
    decision_hash: str
    decision_material_json: str
    candidate_set_fingerprint: str


_INTENT_TYPE_MAP: dict[str, ReconciliationTransactionType] = {
    "expense": ReconciliationTransactionType.EXPENSE,
    "personal_expense": ReconciliationTransactionType.EXPENSE,
    "personal_expense_log": ReconciliationTransactionType.EXPENSE,
    "simple_expense": ReconciliationTransactionType.EXPENSE,
    "simple_expense_log": ReconciliationTransactionType.EXPENSE,
    "shared_expense": ReconciliationTransactionType.EXPENSE,
    "shared_expense_log": ReconciliationTransactionType.EXPENSE,
    "income": ReconciliationTransactionType.INCOME,
    "reimbursement_received": ReconciliationTransactionType.INCOME,
    "dividend_income": ReconciliationTransactionType.INCOME,
    "refund": ReconciliationTransactionType.REFUND,
    "reversal": ReconciliationTransactionType.REVERSAL,
    "chargeback": ReconciliationTransactionType.CHARGEBACK,
    "card_payment": ReconciliationTransactionType.CARD_PAYMENT,
    "payment": ReconciliationTransactionType.CARD_PAYMENT,
    "transfer_in": ReconciliationTransactionType.TRANSFER_IN,
    "transfer_out": ReconciliationTransactionType.TRANSFER_OUT,
    "fee": ReconciliationTransactionType.FEE,
    "investment_fee": ReconciliationTransactionType.FEE,
    "interest_debit": ReconciliationTransactionType.INTEREST_DEBIT,
    "interest_credit": ReconciliationTransactionType.INTEREST_CREDIT,
    "cash_withdrawal": ReconciliationTransactionType.CASH_WITHDRAWAL,
    "cash_deposit": ReconciliationTransactionType.CASH_DEPOSIT,
    "unknown": ReconciliationTransactionType.UNKNOWN,
}

_COMPATIBILITY_MATRIX: dict[StatementAmountDirection, frozenset[ReconciliationTransactionType]] = {
    StatementAmountDirection.DEBIT: frozenset({ReconciliationTransactionType.EXPENSE}),
    StatementAmountDirection.CREDIT: frozenset({ReconciliationTransactionType.INCOME}),
    StatementAmountDirection.REFUND: frozenset({ReconciliationTransactionType.REFUND}),
    StatementAmountDirection.REVERSAL: frozenset({ReconciliationTransactionType.REVERSAL}),
    StatementAmountDirection.CHARGEBACK: frozenset({ReconciliationTransactionType.CHARGEBACK}),
    StatementAmountDirection.PAYMENT: frozenset({ReconciliationTransactionType.CARD_PAYMENT}),
    StatementAmountDirection.CARD_PAYMENT: frozenset({ReconciliationTransactionType.CARD_PAYMENT}),
    StatementAmountDirection.TRANSFER_IN: frozenset({ReconciliationTransactionType.TRANSFER_IN}),
    StatementAmountDirection.TRANSFER_OUT: frozenset({ReconciliationTransactionType.TRANSFER_OUT}),
    StatementAmountDirection.FEE: frozenset({ReconciliationTransactionType.FEE}),
    StatementAmountDirection.INTEREST_DEBIT: frozenset(
        {ReconciliationTransactionType.INTEREST_DEBIT}
    ),
    StatementAmountDirection.INTEREST_CREDIT: frozenset(
        {ReconciliationTransactionType.INTEREST_CREDIT}
    ),
    StatementAmountDirection.CASH_WITHDRAWAL: frozenset(
        {ReconciliationTransactionType.CASH_WITHDRAWAL}
    ),
    StatementAmountDirection.CASH_DEPOSIT: frozenset({ReconciliationTransactionType.CASH_DEPOSIT}),
}

_OUTGOING_DIRECTIONS = frozenset(
    {
        StatementAmountDirection.DEBIT,
        StatementAmountDirection.PAYMENT,
        StatementAmountDirection.CARD_PAYMENT,
        StatementAmountDirection.TRANSFER_OUT,
        StatementAmountDirection.FEE,
        StatementAmountDirection.INTEREST_DEBIT,
        StatementAmountDirection.CASH_WITHDRAWAL,
    }
)
_INCOMING_DIRECTIONS = frozenset(
    {
        StatementAmountDirection.CREDIT,
        StatementAmountDirection.REFUND,
        StatementAmountDirection.REVERSAL,
        StatementAmountDirection.CHARGEBACK,
        StatementAmountDirection.TRANSFER_IN,
        StatementAmountDirection.INTEREST_CREDIT,
        StatementAmountDirection.CASH_DEPOSIT,
    }
)


def normalize_transaction_type(candidate: InternalCandidate) -> ReconciliationTransactionType:
    raw = (
        candidate.transaction_type
        if candidate.transaction_type is not None
        else candidate.source_type
    )
    if isinstance(raw, ReconciliationTransactionType):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return ReconciliationTransactionType.UNKNOWN
    return _INTENT_TYPE_MAP.get(raw.strip().lower(), ReconciliationTransactionType.UNKNOWN)


def check_direction_compatibility(
    direction: StatementAmountDirection | None,
    candidate: InternalCandidate,
) -> DirectionCompatibility:
    transaction_type = normalize_transaction_type(candidate)
    if direction is None:
        return DirectionCompatibility(
            CompatibilityResult.REVIEW_REQUIRED,
            ReasonCode.MISSING_DIRECTION,
            transaction_type,
        )
    if direction in {StatementAmountDirection.UNKNOWN, StatementAmountDirection.INTEREST}:
        return DirectionCompatibility(
            CompatibilityResult.REVIEW_REQUIRED,
            ReasonCode.UNKNOWN_DIRECTION,
            transaction_type,
        )
    if transaction_type is ReconciliationTransactionType.UNKNOWN:
        return DirectionCompatibility(
            CompatibilityResult.REVIEW_REQUIRED,
            ReasonCode.UNKNOWN_TRANSACTION_TYPE,
            transaction_type,
        )
    compatible_types = _COMPATIBILITY_MATRIX.get(direction, frozenset())
    if transaction_type in compatible_types:
        return DirectionCompatibility(
            CompatibilityResult.COMPATIBLE,
            ReasonCode.DIRECTION_TYPE_COMPATIBLE,
            transaction_type,
        )
    return DirectionCompatibility(
        CompatibilityResult.INCOMPATIBLE,
        ReasonCode.DIRECTION_TYPE_INCOMPATIBLE,
        transaction_type,
    )


def original_amount_sign(statement: StatementTransaction) -> OriginalAmountSign:
    if statement.amount == 0:
        return OriginalAmountSign.ZERO
    raw = statement.raw_amount
    if raw is None or not raw.strip():
        return OriginalAmountSign.MISSING
    token = raw.strip()
    if token.startswith("(") and token.endswith(")"):
        return OriginalAmountSign.NEGATIVE
    if token.startswith("-"):
        return OriginalAmountSign.NEGATIVE
    return OriginalAmountSign.POSITIVE


def statement_precheck(statement: StatementTransaction) -> ReasonCode | None:
    if any(
        value is not None and not value.strip()
        for value in (
            statement.public_id,
            statement.row_fingerprint,
            statement.statement_row_reference,
        )
    ):
        return ReasonCode.INVALID_SOURCE_IDENTITY
    if statement.amount_direction is None:
        return ReasonCode.MISSING_DIRECTION
    if statement.amount_direction in {
        StatementAmountDirection.UNKNOWN,
        StatementAmountDirection.INTEREST,
    }:
        return ReasonCode.UNKNOWN_DIRECTION
    if statement.amount == 0:
        return ReasonCode.ZERO_AMOUNT_REQUIRES_REVIEW
    raw_type = (statement.raw_amount_type or "").strip().lower()
    if (
        raw_type in {"credit", "cr", "inflow"}
        and statement.amount_direction in _OUTGOING_DIRECTIONS
    ):
        return ReasonCode.AMOUNT_SIGN_CONTRADICTION
    if (
        raw_type in {"debit", "dr", "outflow"}
        and statement.amount_direction in _INCOMING_DIRECTIONS
    ):
        return ReasonCode.AMOUNT_SIGN_CONTRADICTION
    return None


def evaluate_candidate(
    statement: StatementTransaction,
    candidate: InternalCandidate,
    *,
    date_tolerance_days: int,
    posted_date_window_days: int,
    merchant_similarity_threshold: float,
) -> CandidateEvaluation:
    gates: list[str] = ["source_identity_valid"]
    transaction_type = normalize_transaction_type(candidate)
    if not currency_match(statement.currency, candidate.currency):
        gates.append("currency_incompatible")
        return CandidateEvaluation(
            candidate,
            transaction_type,
            CompatibilityResult.INCOMPATIBLE,
            ReasonCode.CURRENCY_DIFFERS,
            tuple(gates),
        )
    gates.append("currency_compatible")

    compatibility = check_direction_compatibility(statement.amount_direction, candidate)
    if compatibility.result is not CompatibilityResult.COMPATIBLE:
        gates.append(compatibility.reason_code.value)
        return CandidateEvaluation(
            candidate,
            transaction_type,
            compatibility.result,
            compatibility.reason_code,
            tuple(gates),
        )
    gates.append(ReasonCode.DIRECTION_TYPE_COMPATIBLE.value)

    if statement.amount is None or not amount_exact_match(statement.amount, candidate.amount):
        gates.append(ReasonCode.AMOUNT_DIFFERS.value)
        return CandidateEvaluation(
            candidate,
            transaction_type,
            compatibility.result,
            ReasonCode.AMOUNT_DIFFERS,
            tuple(gates),
        )
    gates.append(ReasonCode.EXACT_AMOUNT_MATCH.value)

    date_result = match_date_window(
        candidate.transaction_date,
        statement.transaction_date,
        statement.posted_date,
        date_tolerance_days=date_tolerance_days,
        posted_date_window_days=posted_date_window_days,
    )
    if not date_result.matched:
        gates.append(ReasonCode.OUTSIDE_DATE_TOLERANCE.value)
        return CandidateEvaluation(
            candidate,
            transaction_type,
            compatibility.result,
            ReasonCode.OUTSIDE_DATE_TOLERANCE,
            tuple(gates),
            date_result=date_result,
        )
    gates.append(f"date_compatible:{date_result.matched_on}")

    statement_normalized = statement.merchant_normalized or normalize_merchant(
        statement.merchant_raw
    )
    candidate_normalized = normalize_merchant(candidate.merchant)
    exact = statement.merchant_raw.strip().lower() == candidate.merchant.strip().lower()
    normalized_match = statement_normalized.lower() == candidate_normalized.lower()
    score = Decimal(str(merchant_similarity(statement_normalized, candidate_normalized)))
    if not (exact or normalized_match or score >= Decimal(str(merchant_similarity_threshold))):
        gates.append(ReasonCode.WEAK_MERCHANT_MATCH.value)
        return CandidateEvaluation(
            candidate,
            transaction_type,
            compatibility.result,
            ReasonCode.WEAK_MERCHANT_MATCH,
            tuple(gates),
            date_result=date_result,
            statement_merchant_normalized=statement_normalized,
            candidate_merchant_normalized=candidate_normalized,
            merchant_similarity_score=score,
            merchant_exact=exact,
            merchant_normalized_match=normalized_match,
        )
    gates.append("merchant_compatible")
    return CandidateEvaluation(
        candidate,
        transaction_type,
        compatibility.result,
        ReasonCode.DIRECTION_TYPE_COMPATIBLE,
        tuple(gates),
        date_result=date_result,
        statement_merchant_normalized=statement_normalized,
        candidate_merchant_normalized=candidate_normalized,
        merchant_similarity_score=score,
        merchant_exact=exact,
        merchant_normalized_match=normalized_match,
        eligible=True,
    )


def derive_statement_identity(statement: StatementTransaction) -> str:
    for value in (
        statement.public_id,
        statement.row_fingerprint,
        statement.statement_row_reference,
    ):
        if value is not None and value.strip():
            return value.strip()
    material = {
        "contract_version": _STATEMENT_ID_DOMAIN,
        "source_batch_id": statement.source_batch_id,
        "transaction_date": _date_text(statement.transaction_date),
        "posted_date": _date_text(statement.posted_date),
        "merchant_fingerprint": _text_fingerprint(statement.merchant_raw),
        "amount": _decimal_text(statement.amount),
        "currency": statement.currency,
        "direction": _direction_text(statement.amount_direction),
        "raw_amount": statement.raw_amount,
    }
    return _domain_hash(_STATEMENT_ID_DOMAIN, canonical_json_text(material))


def build_decision_record(
    statement: StatementTransaction,
    result: MatchResult,
    evaluations: Sequence[CandidateEvaluation],
    *,
    date_tolerance_days: int,
    posted_date_window_days: int,
    merchant_similarity_threshold: float,
    matcher_version: str = MATCHER_VERSION,
    compatibility_version: str = COMPATIBILITY_VERSION,
    merchant_normalization_version: str = MERCHANT_NORMALIZATION_VERSION,
    authorization_public_id: str | None = None,
) -> DecisionRecord:
    candidate_material = sorted(
        (_candidate_material(evaluation) for evaluation in evaluations),
        key=lambda item: str(item["candidate_public_id"]),
    )
    candidate_set_material = {
        "candidate_set_version": CANDIDATE_SET_VERSION,
        "candidates": candidate_material,
    }
    candidate_set_json = canonical_json_text(candidate_set_material)
    candidate_set_fingerprint = _domain_hash(_CANDIDATE_HASH_DOMAIN, candidate_set_json)
    material = {
        "decision_contract_version": DECISION_CONTRACT_VERSION,
        "matcher_version": matcher_version,
        "compatibility_version": compatibility_version,
        "merchant_normalization_version": merchant_normalization_version,
        "statement": {
            "derived_identity": derive_statement_identity(statement),
            "public_id": statement.public_id,
            "row_fingerprint": statement.row_fingerprint,
            "source_content_hash": statement.source_content_hash,
            "source_batch_id": statement.source_batch_id,
            "row_reference": statement.statement_row_reference,
            "original_amount_text": statement.raw_amount,
            "original_amount_type": statement.raw_amount_type,
            "original_amount_sign": original_amount_sign(statement).value,
            "normalized_amount": _decimal_text(statement.amount),
            "currency": statement.currency,
            "direction": _direction_text(statement.amount_direction),
            "transaction_date": _date_text(statement.transaction_date),
            "posted_date": _date_text(statement.posted_date),
            "merchant_fingerprint": _text_fingerprint(statement.merchant_raw),
            "merchant_normalized": statement.merchant_normalized
            or normalize_merchant(statement.merchant_raw),
        },
        "thresholds": {
            "date_tolerance_days": date_tolerance_days,
            "posted_date_window_days": posted_date_window_days,
            "merchant_similarity_threshold": str(merchant_similarity_threshold),
        },
        "candidate_set_fingerprint": candidate_set_fingerprint,
        "candidates": candidate_material,
        "final_decision": {
            "status": result.status.value,
            "reason_codes": [reason.value for reason in result.reasons],
            "best_candidate_public_id": result.best_candidate.internal_id
            if result.best_candidate
            else None,
            "authorization_public_id": authorization_public_id,
        },
    }
    material_json = canonical_json_text(material)
    return DecisionRecord(
        decision_hash=_domain_hash(_DECISION_HASH_DOMAIN, material_json),
        decision_material_json=material_json,
        candidate_set_fingerprint=candidate_set_fingerprint,
    )


def verify_persisted_decision_hash(decision_hash: str, decision_material_json: str) -> bool:
    if not _is_sha256(decision_hash):
        return False
    try:
        decoded = json.loads(decision_material_json)
        if not isinstance(decoded, dict) or set(decoded) != {"contract_version", "value"}:
            return False
        if decoded["contract_version"] != "finance-canonical-json-v1":
            return False
        canonical = canonical_json_text(decoded["value"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if canonical != decision_material_json:
        return False
    expected = _domain_hash(_DECISION_HASH_DOMAIN, canonical)
    return hmac.compare_digest(expected, decision_hash)


def verify_authoritative_decision_record(
    decision_row: Any,
    statement_row: Any,
) -> bool:
    """Reconstruct proof material from relational authority and fail closed.

    Candidate evaluation and threshold evidence have no separate relational
    fact source, so their canonical values remain in the proof. Every field
    that does have relational authority is rebuilt from the persisted decision,
    referenced statement row, and owning batch before the hash is checked.
    """
    try:
        decision_hash = str(decision_row["decision_hash"])
        material_json = str(decision_row["decision_material_json"])
        if not verify_persisted_decision_hash(decision_hash, material_json):
            return False
        decoded = json.loads(material_json)
        material = decoded["value"]
        if not isinstance(material, dict):
            return False
        candidates = material["candidates"]
        thresholds = material["thresholds"]
        if not isinstance(candidates, list) or not isinstance(thresholds, dict):
            return False
        candidate_set_json = canonical_json_text(
            {
                "candidate_set_version": CANDIDATE_SET_VERSION,
                "candidates": candidates,
            }
        )
        candidate_set_fingerprint = _domain_hash(
            _CANDIDATE_HASH_DOMAIN,
            candidate_set_json,
        )
        if candidate_set_fingerprint != decision_row["candidate_set_fingerprint"]:
            return False

        direction = (
            StatementAmountDirection(statement_row["amount_direction"])
            if statement_row["amount_direction"] is not None
            else None
        )
        statement = StatementTransaction(
            transaction_date=_date_from_text(statement_row["transaction_date"]),
            posted_date=_date_from_text(statement_row["posted_date"]),
            merchant_raw=str(statement_row["merchant_raw"]),
            merchant_normalized=statement_row["merchant_normalized"],
            amount=Decimal(str(statement_row["amount"])),
            currency=statement_row["currency"],
            account_name=statement_row["account_name"],
            account_id=(
                str(statement_row["account_id"])
                if statement_row["account_id"] is not None
                else None
            ),
            source_batch_id=str(statement_row["batch_id"]),
            statement_row_reference=statement_row["statement_row_reference"],
            public_id=statement_row["public_id"],
            row_fingerprint=statement_row["row_fingerprint"],
            source_content_hash=statement_row["source_content_hash"],
            amount_direction=direction,
            raw_amount=statement_row["raw_amount"],
            raw_amount_type=statement_row["raw_amount_type"],
        )
        reasons = json.loads(decision_row["reason_codes_json"])
        evidence = json.loads(decision_row["evidence_json"])
        if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
            return False
        if not isinstance(evidence, dict):
            return False
        best_candidate_id = decision_row["internal_candidate_id"]
        if not all(
            isinstance(item, dict) and isinstance(item.get("candidate_public_id"), str)
            for item in candidates
        ):
            return False
        candidate_ids = [str(item["candidate_public_id"]) for item in candidates]
        if len(set(candidate_ids)) != len(candidate_ids):
            return False
        if best_candidate_id is not None and best_candidate_id not in candidate_ids:
            return False

        reconstructed = {
            "decision_contract_version": decision_row["decision_contract_version"],
            "matcher_version": decision_row["matcher_version"],
            "compatibility_version": decision_row["compatibility_version"],
            "merchant_normalization_version": decision_row["merchant_normalization_version"],
            "statement": {
                "derived_identity": derive_statement_identity(statement),
                "public_id": statement.public_id,
                "row_fingerprint": statement.row_fingerprint,
                "source_content_hash": statement.source_content_hash,
                "source_batch_id": statement.source_batch_id,
                "row_reference": statement.statement_row_reference,
                "original_amount_text": statement.raw_amount,
                "original_amount_type": statement.raw_amount_type,
                "original_amount_sign": original_amount_sign(statement).value,
                "normalized_amount": _decimal_text(statement.amount),
                "currency": statement.currency,
                "direction": _direction_text(statement.amount_direction),
                "transaction_date": _date_text(statement.transaction_date),
                "posted_date": _date_text(statement.posted_date),
                "merchant_fingerprint": _text_fingerprint(statement.merchant_raw),
                "merchant_normalized": statement.merchant_normalized
                or normalize_merchant(statement.merchant_raw),
            },
            "thresholds": thresholds,
            "candidate_set_fingerprint": candidate_set_fingerprint,
            "candidates": candidates,
            "final_decision": {
                "status": decision_row["match_status"],
                "reason_codes": reasons,
                "best_candidate_public_id": best_candidate_id,
                "authorization_public_id": decision_row["authorization_public_id"],
            },
        }
        if canonical_json_text(reconstructed) != material_json:
            return False
        if bool(decision_row["needs_review"]) != (
            decision_row["match_status"] != MatchStatus.MATCHED.value
        ):
            return False
        return _evidence_matches_statement(
            evidence,
            statement,
            decision_row=decision_row,
            candidates=candidates,
            candidate_ids=candidate_ids,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _evidence_matches_statement(
    evidence: dict[str, object],
    statement: StatementTransaction,
    *,
    decision_row: Any,
    candidates: list[object],
    candidate_ids: list[str],
) -> bool:
    try:
        statement_amount = evidence.get("statement_amount")
        evidence_candidate_ids = evidence.get("candidate_ids")
        candidate_count = evidence.get("candidate_count")
        if (
            not isinstance(evidence_candidate_ids, list)
            or not all(isinstance(item, str) for item in evidence_candidate_ids)
            or len(set(evidence_candidate_ids)) != len(evidence_candidate_ids)
            or not set(evidence_candidate_ids).issubset(candidate_ids)
            or type(candidate_count) is not int
            or candidate_count < 0
            or candidate_count > len(candidate_ids)
        ):
            return False
        candidate_amount = evidence.get("candidate_amount")
        expected_amount_delta = (
            abs(Decimal(str(statement_amount)) - Decimal(str(candidate_amount)))
            if statement_amount is not None and candidate_amount is not None
            else None
        )
        if (
            not _optional_decimal_equal(decision_row["amount_delta"], expected_amount_delta)
            or decision_row["date_delta_days"] != evidence.get("date_delta_days")
            or not _optional_decimal_equal(
                decision_row["merchant_similarity"],
                evidence.get("merchant_similarity"),
            )
            or not _candidate_evidence_matches(
                evidence,
                candidates=candidates,
                evidence_candidate_ids=evidence_candidate_ids,
            )
        ):
            return False
        return (
            (statement_amount is None and statement.amount is None)
            or Decimal(str(statement_amount)) == statement.amount
        ) and all(
            (
                evidence.get("statement_currency") == statement.currency,
                evidence.get("statement_txn_date") == _date_text(statement.transaction_date),
                evidence.get("statement_posted_date") == _date_text(statement.posted_date),
                evidence.get("statement_merchant") == statement.merchant_raw,
                evidence.get("statement_direction") == _direction_text(statement.amount_direction),
                evidence.get("original_amount_text") == statement.raw_amount,
                evidence.get("original_amount_sign") == original_amount_sign(statement).value,
            )
        )
    except (TypeError, ValueError):
        return False


def _candidate_evidence_matches(
    evidence: dict[str, object],
    *,
    candidates: list[object],
    evidence_candidate_ids: list[object],
) -> bool:
    detail_keys = (
        "candidate_amount",
        "candidate_currency",
        "candidate_txn_date",
        "candidate_merchant",
        "candidate_merchant_normalized",
        "candidate_transaction_type",
    )
    if not any(evidence.get(key) is not None for key in detail_keys):
        return True
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_id = candidate.get("candidate_public_id")
        candidate_merchant = evidence.get("candidate_merchant")
        if (
            candidate_id in evidence_candidate_ids
            and _optional_decimal_equal(
                candidate.get("amount"),
                evidence.get("candidate_amount"),
            )
            and candidate.get("currency") == evidence.get("candidate_currency")
            and candidate.get("transaction_date") == evidence.get("candidate_txn_date")
            and isinstance(candidate_merchant, str)
            and candidate.get("merchant_fingerprint") == _text_fingerprint(candidate_merchant)
            and candidate.get("merchant_normalized")
            == evidence.get("candidate_merchant_normalized")
            and candidate.get("transaction_type") == evidence.get("candidate_transaction_type")
            and _optional_decimal_equal(
                candidate.get("merchant_similarity_score"),
                evidence.get("merchant_similarity"),
            )
            and candidate.get("date_delta_days") == evidence.get("date_delta_days")
            and candidate.get("hard_gate_results") == evidence.get("hard_gate_results")
            and candidate.get("compatibility_result") == evidence.get("compatibility_result")
        ):
            return True
    return False


def _optional_decimal_equal(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (ValueError, ArithmeticError):
        return False


def _candidate_material(evaluation: CandidateEvaluation) -> dict[str, object]:
    candidate = evaluation.candidate
    return {
        "candidate_public_id": candidate.internal_id,
        "transaction_type": evaluation.transaction_type.value,
        "transaction_date": candidate.transaction_date.isoformat(),
        "posted_date": _date_text(candidate.posted_date),
        "amount": canonical_decimal_str(candidate.amount),
        "currency": candidate.currency,
        "merchant_fingerprint": _text_fingerprint(candidate.merchant),
        "merchant_normalized": evaluation.candidate_merchant_normalized,
        "merchant_similarity_score": _decimal_text(evaluation.merchant_similarity_score),
        "date_delta_days": evaluation.date_result.day_delta
        if evaluation.date_result is not None
        else None,
        "comparison_date_source": evaluation.date_result.matched_on
        if evaluation.date_result is not None
        else None,
        "hard_gate_results": list(evaluation.hard_gate_results),
        "compatibility_result": evaluation.compatibility.value,
        "compatibility_reason": evaluation.compatibility_reason.value,
        "eligible": evaluation.eligible,
        "evidence_reference": candidate.evidence_reference,
    }


def _decimal_text(value: Decimal | None) -> str | None:
    return canonical_decimal_str(value) if value is not None else None


def _direction_text(value: StatementAmountDirection | None) -> str | None:
    return value.value if value is not None else None


def _date_text(value: object) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def _date_from_text(value: object) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(str(value))


def _text_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _domain_hash(domain: str, payload_text: str) -> str:
    return hashlib.sha256(
        domain.encode("ascii") + b"\x00" + payload_text.encode("utf-8")
    ).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


__all__ = [
    "CANDIDATE_SET_VERSION",
    "COMPATIBILITY_VERSION",
    "DECISION_CONTRACT_VERSION",
    "MATCHER_VERSION",
    "MERCHANT_NORMALIZATION_VERSION",
    "CandidateEvaluation",
    "CompatibilityResult",
    "DecisionRecord",
    "DirectionCompatibility",
    "OriginalAmountSign",
    "build_decision_record",
    "check_direction_compatibility",
    "derive_statement_identity",
    "evaluate_candidate",
    "normalize_transaction_type",
    "original_amount_sign",
    "statement_precheck",
    "verify_authoritative_decision_record",
    "verify_persisted_decision_hash",
]
