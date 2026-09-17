"""Tests for Reconciliation Foundation v1.

Covers:
  1. Exact match
  2. Date tolerance match (posted_date within window)
  3. Amount mismatch
  4. Currency mismatch
  5. Merchant normalization
  6. No internal record
  7. Possible duplicate
  8. Weak merchant match
  9. Missing date fields
 10. Deterministic result
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finance_core.reconciliation import (
    InternalCandidate,
    MatchStatus,
    ReasonCode,
    StatementAmountDirection,
    StatementTransaction,
    hash_result,
    match_statement,
    normalize_merchant,
)

# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

TODAY = date(2024, 12, 1)
T_P1 = date(2024, 11, 30)  # 1 day before
T_P3 = date(2024, 11, 28)  # 3 days before
T_P5 = date(2024, 11, 26)  # 5 days before (outside 3-day tolerance)
T_F1 = date(2024, 12, 2)  # 1 day after
T_F4 = date(2024, 12, 5)  # 4 days after (outside 3-day tolerance)


def _stmt(**kw) -> StatementTransaction:
    defaults = dict(
        transaction_date=TODAY,
        posted_date=None,
        merchant_raw="Apple",
        merchant_normalized=None,
        amount=Decimal("29.90"),
        currency="SGD",
        amount_direction=StatementAmountDirection.DEBIT,
        raw_amount="29.90",
    )
    defaults.update(kw)
    return StatementTransaction(**defaults)


def _cand(internal_id: str, **kw) -> InternalCandidate:
    defaults = dict(
        internal_id=internal_id,
        transaction_date=TODAY,
        merchant="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        source_type="expense",
    )
    defaults.update(kw)
    return InternalCandidate(**defaults)


# ---------------------------------------------------------------------------
# 1. Exact match
# ---------------------------------------------------------------------------


def test_exact_match():
    stmt = _stmt()
    cand = _cand("rec-001")
    result = match_statement(stmt, [cand])

    assert result.status == MatchStatus.MATCHED
    assert result.is_matched
    assert ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE in result.reasons
    assert ReasonCode.EXACT_AMOUNT_MATCH in result.reasons
    assert ReasonCode.SAME_CURRENCY in result.reasons
    assert ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE in result.reasons
    assert ReasonCode.MERCHANT_EXACT_MATCH in result.reasons
    assert result.best_candidate == cand


# ---------------------------------------------------------------------------
# 2. Date tolerance match (posted date only)
# ---------------------------------------------------------------------------


def test_date_tolerance_posted_date_falls_back():
    """Statement has no transaction_date but has posted_date 1 day after."""
    stmt = _stmt(transaction_date=None, posted_date=T_F1)
    cand = _cand("rec-002", transaction_date=TODAY)
    result = match_statement(stmt, [cand])

    assert result.status == MatchStatus.MATCHED
    assert result.is_matched
    assert ReasonCode.POSTED_DATE_WINDOW_MATCH in result.reasons
    assert ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE not in result.reasons


def test_date_tolerance_within_3_days():
    """Statement transaction_date 3 days before candidate -> DATE_MISMATCH
    under exact-match semantics (transaction_date must equal app_date in
    Statement Matching Window v1)."""
    stmt = _stmt(transaction_date=T_P3)
    cand = _cand("rec-003", transaction_date=TODAY)
    result = match_statement(stmt, [cand])

    assert result.status == MatchStatus.DATE_MISMATCH
    assert ReasonCode.OUTSIDE_DATE_TOLERANCE in result.reasons
    assert ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE not in result.reasons


def test_date_tolerance_outside_3_days():
    """Statement posted_date is 5 days before -> date_mismatch."""
    stmt = _stmt(transaction_date=T_P5)
    cand = _cand("rec-004", transaction_date=TODAY)
    result = match_statement(stmt, [cand])

    assert result.status == MatchStatus.DATE_MISMATCH
    assert ReasonCode.OUTSIDE_DATE_TOLERANCE in result.reasons


# ---------------------------------------------------------------------------
# 3. Amount mismatch
# ---------------------------------------------------------------------------


def test_amount_mismatch():
    stmt = _stmt(amount=Decimal("50.00"))
    cand = _cand("rec-005", amount=Decimal("29.90"))
    result = match_statement(stmt, [cand])

    assert result.status == MatchStatus.AMOUNT_MISMATCH
    assert ReasonCode.AMOUNT_DIFFERS in result.reasons


# ---------------------------------------------------------------------------
# 4. Currency mismatch
# ---------------------------------------------------------------------------


def test_currency_mismatch():
    stmt = _stmt(currency="USD")
    cand = _cand("rec-006", currency="SGD")
    result = match_statement(stmt, [cand])

    assert result.status == MatchStatus.CURRENCY_MISMATCH
    assert ReasonCode.CURRENCY_DIFFERS in result.reasons


# ---------------------------------------------------------------------------
# 5. Merchant normalization
# ---------------------------------------------------------------------------


def test_merchant_normalization_match():
    """APPLE.COM/BILL should normalize to 'apple' and match."""
    stmt = _stmt(merchant_raw="APPLE.COM/BILL")
    cand = _cand("rec-007", merchant="Apple")
    result = match_statement(stmt, [cand])

    assert result.status == MatchStatus.MATCHED
    assert ReasonCode.MERCHANT_NORMALIZED_MATCH in result.reasons


def test_merchant_normalization_fallback():
    """'The Coffee Shop' vs 'Coffee Shop' has 0.5 similarity -> matches."""
    stmt = _stmt(merchant_raw="The Coffee Shop")
    cand = _cand("rec-008", merchant="Coffee Shop")
    result = match_statement(stmt, [cand])

    assert result.status == MatchStatus.MATCHED
    assert ReasonCode.MERCHANT_SIMILARITY_MATCH in result.reasons


# ---------------------------------------------------------------------------
# 6. No internal record
# ---------------------------------------------------------------------------


def test_no_internal_record():
    stmt = _stmt()
    result = match_statement(stmt, [])

    assert result.status == MatchStatus.NO_MATCH
    assert ReasonCode.NO_CANDIDATE_FOUND in result.reasons


# ---------------------------------------------------------------------------
# 7. Possible duplicate
# ---------------------------------------------------------------------------


def test_possible_duplicate():
    stmt = _stmt()
    cand1 = _cand("rec-009a")
    cand2 = _cand("rec-009b")  # identical amount, currency, date
    result = match_statement(stmt, [cand1, cand2])

    assert result.status == MatchStatus.POSSIBLE_DUPLICATE
    assert ReasonCode.MULTIPLE_CANDIDATE_MATCHES in result.reasons


def test_ambiguous_multiple_different():
    """Two candidates match amount/currency/date but different merchants."""
    stmt = _stmt(merchant_raw="Apple")
    cand1 = _cand("rec-010a", merchant="Apple Store")
    cand2 = _cand("rec-010b", merchant="Apple Online")
    result = match_statement(stmt, [cand1, cand2])

    assert result.status == MatchStatus.AMBIGUOUS
    assert ReasonCode.MULTIPLE_CANDIDATE_MATCHES in result.reasons


# ---------------------------------------------------------------------------
# 8. Weak merchant match
# ---------------------------------------------------------------------------


def test_weak_merchant_match():
    """Merchants have no similarity -> merchant_mismatch."""
    stmt = _stmt(merchant_raw="Apple")
    cand = _cand("rec-011", merchant="Netflix")
    result = match_statement(stmt, [cand])

    assert result.status == MatchStatus.MERCHANT_MISMATCH
    assert ReasonCode.WEAK_MERCHANT_MATCH in result.reasons


# ---------------------------------------------------------------------------
# 9. Missing date fields
# ---------------------------------------------------------------------------


def test_missing_statement_dates():
    """Both transaction_date and posted_date are None."""
    stmt = _stmt(transaction_date=None, posted_date=None)
    cand = _cand("rec-012")
    result = match_statement(stmt, [cand])

    assert result.status == MatchStatus.NEEDS_REVIEW
    assert ReasonCode.MISSING_STATEMENT_DATE in result.reasons


# ---------------------------------------------------------------------------
# 10. Deterministic result
# ---------------------------------------------------------------------------


def test_deterministic_result():
    """Same inputs must produce the same stable SHA-256 digest."""
    stmt = _stmt()
    cand = _cand("rec-013")
    result = match_statement(stmt, [cand])
    digest = hash_result(result)

    # SHA-256 hex digest is always 64 lowercase hex characters.
    assert isinstance(digest, str)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)

    # Repeated calls produce the same digest.
    for _ in range(4):
        assert hash_result(match_statement(stmt, [cand])) == digest

    # Assert a known fixed digest for this exact input.
    assert digest == "5a7080f5a55f168df7fb8e74aa46d623f68270d2de011a082e8eac547e906a29"


# ---------------------------------------------------------------------------
# Model validation edge cases
# ---------------------------------------------------------------------------


def test_statement_transaction_negative_amount_raises():
    with pytest.raises(ValueError, match="positive"):
        StatementTransaction(
            transaction_date=TODAY,
            posted_date=None,
            merchant_raw="x",
            amount=Decimal("-10.00"),
        )


def test_internal_candidate_negative_amount_raises():
    with pytest.raises(ValueError, match="positive"):
        InternalCandidate(
            internal_id="x",
            transaction_date=TODAY,
            merchant="x",
            amount=Decimal("-10.00"),
            currency="SGD",
        )


# ---------------------------------------------------------------------------
# Merchant normalizer unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("APPLE.COM/BILL", "apple"),
        ("  Apple  ", "apple"),
        ("Google *YouTube", "google"),
        ("Netflix.com", "netflix"),
        ("Unknown Merchant XYZ", "unknown merchant xyz"),
    ],
)
def test_normalize_merchant(raw, expected):
    assert normalize_merchant(raw) == expected
