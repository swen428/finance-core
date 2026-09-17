"""Focused tests for statement transaction_date vs posted_date priority v1.

Pin the v1 reconciliation contract for statement sources that provide both a
``transaction_date`` (when the charge occurred) and a ``posted_date`` (when the
bank/card posted or settled it).  These tests document and guard the current
behaviour; they introduce no new behaviour.

Contract pinned here:

  (a) Matching uses ``transaction_date`` when both dates are present.
  (b) Matching falls back to ``posted_date`` only when ``transaction_date``
      is missing (exact match or ``posted_date_window_days`` lookback).
  (c) ``transaction_date`` is **exact-match only** in Statement Matching
      Window v1 — a ``transaction_date`` 1 day off is ``DATE_MISMATCH``.
      Applying a tolerance window to ``transaction_date`` is intentionally
      out of scope for v1; it may be added later as a separate opt-in
      matching mode (see
      docs/design/reconciliation_foundation_v1.md — Date Source Priority).
  (d) A ``posted_date`` within tolerance must **not** override or rescue a
      ``transaction_date`` mismatch when ``transaction_date`` is present.
  (e) Existing CSV-only posted-date import behaviour is backward compatible:
      a row with only ``posted_date`` parses to ``transaction_date=None`` and
      zero warnings; a row with both dates preserves both independently.

The matcher is a pure deterministic function; no records are created,
updated, or deleted by anything exercised here.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from finance_core.reconciliation.matcher import match_statement
from finance_core.reconciliation.models import (
    InternalCandidate,
    MatchStatus,
    ReasonCode,
    StatementAmountDirection,
    StatementTransaction,
)
from finance_core.reconciliation.statement_csv import StatementCsvAdapter

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
# (a) Matching uses transaction_date when both dates are present
# ===================================================================


class TestTransactionDatePreferredWhenPresent:
    """When statement.transaction_date is present, it is the matching date."""

    def test_both_dates_present_matches_on_transaction_date(self):
        stmt = _stmt(
            transaction_date=date(2024, 5, 24),
            posted_date=date(2024, 5, 26),
        )
        cand = _cand("c1", transaction_date=date(2024, 5, 24))
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.MATCHED
        assert ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE in result.reasons
        # Posted-date reason codes must not appear when transaction_date matched.
        assert ReasonCode.POSTED_DATE_WITHIN_TOLERANCE not in result.reasons
        assert ReasonCode.POSTED_DATE_WINDOW_MATCH not in result.reasons

    def test_both_dates_present_exact_txn_match_uses_txn_date(self):
        """Even when posted_date equals app_date, the match is attributed to
        transaction_date because transaction_date is present and exact."""
        stmt = _stmt(
            transaction_date=date(2024, 5, 24),
            posted_date=date(2024, 5, 25),  # equals app_date
        )
        cand = _cand("c1", transaction_date=date(2024, 5, 25))
        result = match_statement(stmt, [cand])
        # transaction_date (May 24) != app_date (May 25) -> DATE_MISMATCH
        assert result.status == MatchStatus.DATE_MISMATCH
        assert ReasonCode.POSTED_DATE_WITHIN_TOLERANCE not in result.reasons


# ===================================================================
# (b) posted_date fallback only when transaction_date is missing
# ===================================================================


class TestPostedDateFallbackOnlyWhenTxnDateMissing:
    """posted_date is used only when statement.transaction_date is None."""

    def test_only_posted_date_exact_match(self):
        stmt = _stmt(transaction_date=None, posted_date=date(2024, 5, 26))
        cand = _cand("c1", transaction_date=date(2024, 5, 26))
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.MATCHED
        assert ReasonCode.POSTED_DATE_WITHIN_TOLERANCE in result.reasons
        assert ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE not in result.reasons

    def test_only_posted_date_window_match(self):
        stmt = _stmt(transaction_date=None, posted_date=date(2024, 5, 26))
        cand = _cand("c1", transaction_date=date(2024, 5, 24))  # 2 days before
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.MATCHED
        assert ReasonCode.POSTED_DATE_WINDOW_MATCH in result.reasons

    def test_only_posted_date_outside_window_no_match(self):
        stmt = _stmt(transaction_date=None, posted_date=date(2024, 5, 26))
        cand = _cand("c1", transaction_date=date(2024, 5, 20))  # 6 days before
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.DATE_MISMATCH


# ===================================================================
# (c) transaction_date exact-match only — tolerance is out of scope for v1
# ===================================================================


class TestTransactionDateExactMatchOnly:
    """Statement Matching Window v1 applies NO tolerance to transaction_date.

    A transaction_date 1 day off is a DATE_MISMATCH.  Applying a tolerance
    window to transaction_date is intentionally out of scope for v1 and may
    be considered later as a separate opt-in matching mode — see
    docs/design/reconciliation_foundation_v1.md (Date Source Priority).
    """

    def test_txn_date_one_day_off_is_date_mismatch(self):
        stmt = _stmt(
            transaction_date=date(2024, 5, 24),
            posted_date=date(2024, 5, 24),
        )
        cand = _cand("c1", transaction_date=date(2024, 5, 25))  # 1 day off
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.DATE_MISMATCH
        assert ReasonCode.OUTSIDE_DATE_TOLERANCE in result.reasons
        assert ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE not in result.reasons

    def test_txn_date_two_days_off_is_date_mismatch(self):
        stmt = _stmt(transaction_date=date(2024, 5, 24))
        cand = _cand("c1", transaction_date=date(2024, 5, 26))  # 2 days off
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.DATE_MISMATCH

    def test_txn_date_within_tolerance_does_not_match_v1(self):
        """Explicitly documents that requirement (c) is NOT satisfied in v1
        exact-match mode.  A app_date within date_tolerance_days of
        transaction_date still fails because tolerance is not applied to
        transaction_date in v1."""
        stmt = _stmt(
            transaction_date=date(2024, 5, 24),
            posted_date=date(2024, 6, 1),  # far away / outside any window
        )
        cand = _cand("c1", transaction_date=date(2024, 5, 25))  # 1 day off
        result = match_statement(stmt, [cand], date_tolerance_days=3)
        assert result.status == MatchStatus.DATE_MISMATCH


# ===================================================================
# (d) posted_date within tolerance must not rescue a txn_date mismatch
# ===================================================================


class TestPostedDateDoesNotOverrideTxnDateMismatch:
    """When transaction_date is present and mismatches, a close or exact
    posted_date must NOT rescue the match."""

    def test_posted_date_exact_does_not_override_txn_mismatch(self):
        """app_date == posted_date exactly, but transaction_date differs."""
        stmt = _stmt(
            transaction_date=date(2024, 5, 24),
            posted_date=date(2024, 5, 26),
        )
        cand = _cand("c1", transaction_date=date(2024, 5, 26))  # == posted_date
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.DATE_MISMATCH
        assert ReasonCode.POSTED_DATE_WITHIN_TOLERANCE not in result.reasons
        assert ReasonCode.POSTED_DATE_WINDOW_MATCH not in result.reasons
        assert ReasonCode.TRANSACTION_DATE_WITHIN_TOLERANCE not in result.reasons

    def test_posted_date_outside_tolerance_does_not_override_txn_mismatch(self):
        """posted_date is itself outside tolerance; transaction_date also
        mismatches.  Still DATE_MISMATCH, no posted-date rescue."""
        stmt = _stmt(
            transaction_date=date(2024, 5, 20),
            posted_date=date(2024, 5, 26),
        )
        cand = _cand("c1", transaction_date=date(2024, 5, 26))  # == posted_date
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.DATE_MISMATCH

    def test_posted_date_close_but_txn_off_does_not_match(self):
        """posted_date within the lookback window of app_date, but
        transaction_date is present and off — no match."""
        stmt = _stmt(
            transaction_date=date(2024, 5, 20),
            posted_date=date(2024, 5, 25),
        )
        # app_date May 23 is 2 days before posted_date May 25 (window match)
        # but transaction_date May 20 is present and 3 days off.
        cand = _cand("c1", transaction_date=date(2024, 5, 23))
        result = match_statement(stmt, [cand])
        assert result.status == MatchStatus.DATE_MISMATCH
        assert ReasonCode.POSTED_DATE_WINDOW_MATCH not in result.reasons


# ===================================================================
# (e) CSV-only posted-date import behaviour is backward compatible
# ===================================================================


class TestCsvImportDatePreservationBackwardCompatible:
    """The CSV adapter preserves both dates independently with no backfill
    and emits no warning for posted-date-only rows."""

    def _parse(self, headers, rows):
        import csv
        import os
        import tempfile
        from pathlib import Path

        fd, path = tempfile.mkstemp(suffix=".csv", prefix="test_date_prio_")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(headers)
            for row in rows:
                writer.writerow(row)
        try:
            return StatementCsvAdapter().parse_file_hardened(Path(path))
        finally:
            Path(path).unlink(missing_ok=True)

    def test_posted_date_only_txn_date_none_no_warning(self):
        result = self._parse(
            ["posted_date", "merchant_raw", "amount", "currency"],
            [["2024-05-26", "GRAB", "18.80", "SGD"]],
        )
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.transaction_date is None
        assert row.posted_date == date(2024, 5, 26)
        assert len(result.warnings) == 0

    def test_both_dates_preserved_independently(self):
        result = self._parse(
            ["transaction_date", "posted_date", "merchant_raw", "amount", "currency"],
            [["2024-05-24", "2024-05-26", "GRAB", "18.80", "SGD"]],
        )
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.transaction_date == date(2024, 5, 24)
        assert row.posted_date == date(2024, 5, 26)
        assert row.transaction_date != row.posted_date

    def test_transaction_date_only_posted_date_none(self):
        result = self._parse(
            ["transaction_date", "merchant_raw", "amount", "currency"],
            [["2024-05-24", "GRAB", "18.80", "SGD"]],
        )
        assert len(result.rows) == 1
        row = result.rows[0]
        assert row.transaction_date == date(2024, 5, 24)
        assert row.posted_date is None


# ===================================================================
# Determinism / no side effects
# ===================================================================


class TestDeterministicNoSideEffects:
    """match_statement is a pure deterministic function."""

    def test_repeated_match_is_stable(self):
        stmt = _stmt(
            transaction_date=date(2024, 5, 24),
            posted_date=date(2024, 5, 26),
        )
        cand = _cand("c1", transaction_date=date(2024, 5, 24))
        r1 = match_statement(stmt, [cand])
        r2 = match_statement(stmt, [cand])
        assert r1.status == r2.status
        assert r1.reasons == r2.reasons
