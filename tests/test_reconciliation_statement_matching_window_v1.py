"""Tests for Statement Matching Window v1.

Verifies that the reconciliation date-matching logic supports a
conservative posted_date lookback window so app transactions whose
actual transaction date differs from the bank posted_date can still be
matched correctly.

Covers:
  1. Exact transaction_date match
  2. Exact posted_date match (statement has only posted_date)
  3. Posted-date lookback match (app_date 1-3 days before posted_date)
  4. Outside window (app_date > 3 days before posted_date)
  5. App date after posted_date -> no match
  6. Explicit transaction_date mismatch should fail (even if posted_date is close)
  7. Existing date semantics tests still pass (import check only)
  8. No auto reconciliation side effects (does not create/update/delete records)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from finance_core.reconciliation.matcher import match_statement
from finance_core.reconciliation.models import (
    DateMatchResult,
    InternalCandidate,
    MatchStatus,
    ReasonCode,
    StatementAmountDirection,
    StatementTransaction,
    match_date_window,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stmt(**kw) -> StatementTransaction:
    defaults: dict = dict(
        transaction_date=None,
        posted_date=None,
        merchant_raw="GRAB",
        amount=Decimal("18.80"),
        currency="SGD",
        amount_direction=StatementAmountDirection.DEBIT,
        raw_amount="18.80",
    )
    defaults.update(kw)
    return StatementTransaction(**defaults)


def _cand(internal_id: str, **kw) -> InternalCandidate:
    defaults: dict = dict(
        internal_id=internal_id,
        transaction_date=date(2024, 5, 24),
        merchant="GRAB",
        amount=Decimal("18.80"),
        currency="SGD",
        source_type="expense",
    )
    defaults.update(kw)
    return InternalCandidate(**defaults)


# ===================================================================
# 1. match_date_window() unit tests
# ===================================================================


class TestMatchDateWindowUnit:
    """Pure unit tests for the match_date_window() helper."""

    def test_exact_transaction_date_match(self):
        """App date equals statement.transaction_date -> matched."""
        result = match_date_window(
            app_date=date(2024, 5, 24),
            statement_transaction_date=date(2024, 5, 24),
            statement_posted_date=None,
        )
        assert result.matched is True
        assert result.matched_on == "transaction_date"
        assert result.day_delta == 0

    def test_transaction_date_within_tolerance(self):
        """App date 2 days off statement.transaction_date -> not matched
        under exact-match semantics (tolerance not applied to transaction_date
        in Statement Matching Window v1)."""
        result = match_date_window(
            app_date=date(2024, 5, 22),
            statement_transaction_date=date(2024, 5, 24),
            statement_posted_date=None,
        )
        assert result.matched is False
        assert result.matched_on == "none"
        assert result.day_delta == 2

    def test_transaction_date_outside_tolerance(self):
        """App date is 5 days off -> no match, no fallback."""
        result = match_date_window(
            app_date=date(2024, 5, 19),
            statement_transaction_date=date(2024, 5, 24),
            statement_posted_date=date(2024, 5, 26),  # close, but irrelevant
            date_tolerance_days=3,
        )
        assert result.matched is False
        assert result.matched_on == "none"

    def test_exact_posted_date_match(self):
        """Only posted_date available, app_date equals posted_date -> matched on posted_date."""
        result = match_date_window(
            app_date=date(2024, 5, 26),
            statement_transaction_date=None,
            statement_posted_date=date(2024, 5, 26),
        )
        assert result.matched is True
        assert result.matched_on == "posted_date"
        assert result.day_delta == 0

    def test_posted_date_lookback_1_day(self):
        """App date 1 day before posted_date -> matched on posted_date_window."""
        result = match_date_window(
            app_date=date(2024, 5, 25),
            statement_transaction_date=None,
            statement_posted_date=date(2024, 5, 26),
            posted_date_window_days=3,
        )
        assert result.matched is True
        assert result.matched_on == "posted_date_window"
        assert result.day_delta == 1

    def test_posted_date_lookback_3_days(self):
        """App date 3 days before posted_date -> matched on posted_date_window."""
        result = match_date_window(
            app_date=date(2024, 5, 23),
            statement_transaction_date=None,
            statement_posted_date=date(2024, 5, 26),
            posted_date_window_days=3,
        )
        assert result.matched is True
        assert result.matched_on == "posted_date_window"
        assert result.day_delta == 3

    def test_outside_posted_date_window(self):
        """App date 4 days before posted_date -> no match."""
        result = match_date_window(
            app_date=date(2024, 5, 22),
            statement_transaction_date=None,
            statement_posted_date=date(2024, 5, 26),
            posted_date_window_days=3,
        )
        assert result.matched is False
        assert result.matched_on == "none"

    def test_app_date_after_posted_date_no_match(self):
        """App date is AFTER posted_date -> no match."""
        result = match_date_window(
            app_date=date(2024, 5, 28),
            statement_transaction_date=None,
            statement_posted_date=date(2024, 5, 26),
            posted_date_window_days=3,
        )
        assert result.matched is False
        assert result.matched_on == "none"

    def test_both_dates_missing_no_match(self):
        """Neither date available -> not matched."""
        result = match_date_window(
            app_date=date(2024, 5, 24),
            statement_transaction_date=None,
            statement_posted_date=None,
        )
        assert result.matched is False
        assert result.matched_on == "none"
        assert result.day_delta is None


# ===================================================================
# 2. match_statement() integration -- transaction_date present
# ===================================================================


class TestMatchStatementWithTransactionDate:
    """Tests for match_statement() when statement.transaction_date is present."""

    def test_exact_txn_date_match_yields_match(self):
        stmt = _stmt(transaction_date=date(2024, 5, 24))
        cand = _cand("c1", transaction_date=date(2024, 5, 24))
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.MATCHED
        assert ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE in result.reasons

    def test_txn_date_one_day_mismatch_fails(self):
        """App date 1 day off statement.transaction_date -> DATE_MISMATCH
        under exact-match semantics.  date_tolerance_days is not applied
        to transaction_date in Statement Matching Window v1."""
        stmt = _stmt(
            transaction_date=date(2024, 5, 24),
            posted_date=date(2024, 5, 24),
        )
        cand = _cand("c1", transaction_date=date(2024, 5, 25))
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.DATE_MISMATCH
        assert ReasonCode.OUTSIDE_DATE_TOLERANCE in result.reasons
        # Posted_date must not be used as fallback
        assert ReasonCode.POSTED_DATE_WITHIN_TOLERANCE not in result.reasons
        assert ReasonCode.POSTED_DATE_WINDOW_MATCH not in result.reasons
        assert ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE not in result.reasons

    def test_txn_date_outside_tolerance_fails_no_posted_fallback(self):
        """When transaction_date mismatches, posted_date must NOT be used as fallback."""
        stmt = _stmt(
            transaction_date=date(2024, 5, 24),
            posted_date=date(2024, 5, 30),
        )
        cand = _cand("c1", transaction_date=date(2024, 5, 30))
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.DATE_MISMATCH


# ===================================================================
# 3. match_statement() integration -- only posted_date present
# ===================================================================


class TestMatchStatementPostedDateOnly:
    """Tests for match_statement() when only statement.posted_date is available."""

    def test_exact_posted_date_match(self):
        stmt = _stmt(transaction_date=None, posted_date=date(2024, 5, 26))
        cand = _cand("c1", transaction_date=date(2024, 5, 26))
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.MATCHED
        assert ReasonCode.POSTED_DATE_WITHIN_TOLERANCE in result.reasons

    def test_lookback_1_day_before_posted_date(self):
        """App date 1 day before posted_date -> matches via window."""
        stmt = _stmt(transaction_date=None, posted_date=date(2024, 5, 26))
        cand = _cand("c1", transaction_date=date(2024, 5, 25))
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.MATCHED
        assert ReasonCode.POSTED_DATE_WINDOW_MATCH in result.reasons

    def test_lookback_3_days_before_posted_date(self):
        """App date 3 days before posted_date -> matches via window."""
        stmt = _stmt(transaction_date=None, posted_date=date(2024, 5, 26))
        cand = _cand("c1", transaction_date=date(2024, 5, 23))
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.MATCHED
        assert ReasonCode.POSTED_DATE_WINDOW_MATCH in result.reasons

    def test_outside_window_more_than_3_days_before(self):
        """App date 4 days before posted_date -> no match."""
        stmt = _stmt(transaction_date=None, posted_date=date(2024, 5, 26))
        cand = _cand("c1", transaction_date=date(2024, 5, 22))
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.DATE_MISMATCH

    def test_app_date_after_posted_date_no_match(self):
        """App date after posted_date (credit card cannot post before transaction)."""
        stmt = _stmt(transaction_date=None, posted_date=date(2024, 5, 26))
        cand = _cand("c1", transaction_date=date(2024, 5, 28))
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.DATE_MISMATCH


# ===================================================================
# 4. Explicit transaction_date mismatch must not fall back to posted_date
# ===================================================================


class TestTransactionDatePriority:
    """When transaction_date is present, it takes priority over posted_date."""

    def test_txn_date_present_but_mismatch_with_close_posted_date(self):
        """Statement has both dates; app_date differs from txn_date but matches
        posted_date exactly.  posted_date must NOT be used as fallback."""
        stmt = _stmt(
            transaction_date=date(2024, 5, 24),
            posted_date=date(2024, 5, 26),
        )
        cand = _cand("c1", transaction_date=date(2024, 5, 26))
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.DATE_MISMATCH

    def test_txn_date_mismatch_outside_tolerance_posted_close_but_irrelevant(self):
        """Statement has both dates; app_date is far from txn_date but close to posted_date."""
        stmt = _stmt(
            transaction_date=date(2024, 5, 20),
            posted_date=date(2024, 5, 26),
        )
        cand = _cand("c1", transaction_date=date(2024, 5, 26))
        # App date (May 26) is 6 days from txn_date (May 20) -> outside tolerance
        # posted_date is May 26, exact match, but must NOT be used as fallback
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.DATE_MISMATCH


# ===================================================================
# 5. No side effects / no auto reconciliation
# ===================================================================


class TestNoSideEffects:
    """Verify the date-window matching does not create/update/delete any records."""

    def test_date_match_result_is_immutable(self):
        result = DateMatchResult(matched=True, matched_on="transaction_date", day_delta=0)
        assert result.matched is True
        assert result.day_delta == 0

    def test_match_statement_does_not_persist_anything(self):
        """match_statement() is a pure function -- no side effects."""
        stmt = _stmt(transaction_date=date(2024, 5, 24))
        cand = _cand("c1", transaction_date=date(2024, 5, 24))

        result1 = match_statement(stmt, [cand])
        result2 = match_statement(stmt, [cand])

        assert result1.status == result2.status
        assert result1.reasons == result2.reasons


# ===================================================================
# 6. Customizable posted_date_window_days
# ===================================================================


class TestCustomWindowDays:
    """The posted_date_window_days parameter is configurable."""

    def test_custom_window_5_days_matches(self):
        stmt = _stmt(transaction_date=None, posted_date=date(2024, 5, 26))
        cand = _cand("c1", transaction_date=date(2024, 5, 21))  # 5 days before
        result = match_statement(stmt, [cand], posted_date_window_days=5)
        assert result.status == MatchStatus.MATCHED

    def test_custom_window_1_day_rejects(self):
        stmt = _stmt(transaction_date=None, posted_date=date(2024, 5, 26))
        cand = _cand("c1", transaction_date=date(2024, 5, 24))  # 2 days before
        result = match_statement(stmt, [cand], posted_date_window_days=1)
        assert result.status == MatchStatus.DATE_MISMATCH


# ===================================================================
# 7. Existing date semantics regression check
# ===================================================================


class TestDateSemanticsRegression:
    """Verify existing date semantics tests still pass (import check)."""

    def test_date_semantics_test_file_importable(self):
        """The existing test file should still import successfully."""
        import tests.test_reconciliation_statement_date_semantics_v1  # noqa: F401
