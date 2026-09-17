"""Tests for Reconciliation Matching Engine v1 (Review Queue + Resolution).

Covers:
  1. exact match
  2. normalized merchant match
  3. amount mismatch
  4. missing in app
  5. missing in statement
  6. possible duplicate
  7. date mismatch
  8. build_summary correctness
  9. deterministic output
 10. empty input handling
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from finance_core.reconciliation.matching import build_summary, match_batch
from finance_core.reconciliation.models import (
    AppTransaction,
    IssueType,
    StatementAmountDirection,
    StatementTransaction,
)


def _stmt(**kw) -> StatementTransaction:
    defaults: dict = dict(
        transaction_date=date(2024, 12, 1),
        posted_date=None,
        merchant_raw="Apple",
        merchant_normalized=None,
        amount=Decimal("29.90"),
        currency="SGD",
        statement_row_reference="stmt-001",
        amount_direction=StatementAmountDirection.DEBIT,
        raw_amount="29.90",
    )
    defaults.update(kw)
    return StatementTransaction(**defaults)


def _app(app_txn_id: str, **kw) -> AppTransaction:
    defaults: dict = dict(
        app_txn_id=app_txn_id,
        transaction_date=date(2024, 12, 1),
        merchant="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        source_type="expense",
    )
    defaults.update(kw)
    return AppTransaction(**defaults)


# ---------------------------------------------------------------------------
# 1. Exact match
# ---------------------------------------------------------------------------


def test_exact_match():
    stmt = _stmt()
    app = _app("app-001")
    candidates = match_batch([stmt], [app])

    assert len(candidates) == 1
    c = candidates[0]
    assert c.issue_type == IssueType.MATCHED
    assert c.best_app_transaction is not None
    assert c.best_app_transaction.app_txn_id == "app-001"
    assert c.confidence_score == Decimal("1.0")


# ---------------------------------------------------------------------------
# 2. Normalized merchant match
# ---------------------------------------------------------------------------


def test_normalized_merchant_match():
    stmt = _stmt(merchant_raw="apple.com/bill")
    app = _app("app-002", merchant="Apple")
    candidates = match_batch([stmt], [app])

    assert len(candidates) == 1
    c = candidates[0]
    assert c.issue_type == IssueType.MATCHED
    assert c.confidence_score == Decimal("0.8")


# ---------------------------------------------------------------------------
# 3. Amount mismatch
# ---------------------------------------------------------------------------


def test_amount_mismatch():
    stmt = _stmt(amount=Decimal("29.90"))
    app = _app("app-003", amount=Decimal("15.00"))
    candidates = match_batch([stmt], [app])

    assert (
        len(candidates) == 1
    )  # only the amount_mismatch candidate; no spurious missing_in_statement
    assert candidates[0].issue_type == IssueType.AMOUNT_MISMATCH


# ---------------------------------------------------------------------------
# 4. Missing in app
# ---------------------------------------------------------------------------


def test_missing_in_app():
    stmt = _stmt(merchant_raw="UniqueStore", amount=Decimal("100.00"))
    app = _app("app-004", merchant="DifferentStore", amount=Decimal("50.00"))
    candidates = match_batch([stmt], [app])

    # Statement has no match -> should get something other than MATCHED
    stmt_candidates = [c for c in candidates if c.issue_type != IssueType.MISSING_IN_STATEMENT]
    assert len(stmt_candidates) >= 1
    assert stmt_candidates[0].issue_type != IssueType.MATCHED


# ---------------------------------------------------------------------------
# 5. Missing in statement
# ---------------------------------------------------------------------------


def test_missing_in_statement():
    stmt = _stmt()
    app_matched = _app("app-matched")
    app_extra = _app("app-extra", merchant="UnmatchedApp", amount=Decimal("99.99"))
    candidates = match_batch([stmt], [app_matched, app_extra])

    # app_extra should be flagged as missing_in_statement
    missing = [c for c in candidates if c.issue_type == IssueType.MISSING_IN_STATEMENT]
    assert len(missing) == 1
    assert missing[0].best_app_transaction is not None
    assert missing[0].best_app_transaction.app_txn_id == "app-extra"


# ---------------------------------------------------------------------------
# 6. Possible duplicate
# ---------------------------------------------------------------------------


def test_possible_duplicate():
    stmt = _stmt(merchant_raw="Grab", amount=Decimal("8.50"))
    app_a = _app("app-dup-a", merchant="Grab", amount=Decimal("8.50"))
    app_b = _app("app-dup-b", merchant="Grab", amount=Decimal("8.50"))
    candidates = match_batch([stmt], [app_a, app_b])

    stmt_cand = [c for c in candidates if c.issue_type != IssueType.MISSING_IN_STATEMENT]
    assert len(stmt_cand) == 1
    assert stmt_cand[0].issue_type in (
        IssueType.POSSIBLE_DUPLICATE,
        IssueType.NEEDS_REVIEW,
    )


def test_duplicate_statement_rows_matching_same_app_are_flagged_for_review():
    """One app transaction must not be accepted as matched by two statement rows."""
    stmt_a = _stmt(statement_row_reference="dup-stmt-a")
    stmt_b = _stmt(statement_row_reference="dup-stmt-b")
    app = _app("app-shared")

    candidates = match_batch([stmt_a, stmt_b], [app])

    assert len(candidates) == 2
    assert [c.issue_type for c in candidates] == [
        IssueType.POSSIBLE_DUPLICATE,
        IssueType.POSSIBLE_DUPLICATE,
    ]
    assert all(c.best_app_transaction is not None for c in candidates)
    assert {c.best_app_transaction.app_txn_id for c in candidates if c.best_app_transaction} == {
        "app-shared"
    }


# ---------------------------------------------------------------------------
# 7. Date mismatch
# ---------------------------------------------------------------------------


def test_date_mismatch():
    stmt = _stmt(transaction_date=date(2024, 12, 15))
    app = _app("app-007", transaction_date=date(2024, 12, 1))
    candidates = match_batch([stmt], [app])

    # date mismatch — outside 3-day window
    stmt_cand = [c for c in candidates if c.issue_type != IssueType.MISSING_IN_STATEMENT]
    assert len(stmt_cand) == 1
    assert stmt_cand[0].issue_type == IssueType.DATE_MISMATCH


# ---------------------------------------------------------------------------
# 7b. Regression: mismatch-referenced apps do NOT also get missing_in_statement
# ---------------------------------------------------------------------------


def test_mismatch_referenced_app_no_missing_in_statement():
    """amount_mismatch must not also produce missing_in_statement for the same app."""
    stmt = _stmt(amount=Decimal("29.90"))
    app = _app("app-mismatch", amount=Decimal("15.00"))
    candidates = match_batch([stmt], [app])

    assert len(candidates) == 1
    assert candidates[0].issue_type == IssueType.AMOUNT_MISMATCH
    assert candidates[0].best_app_transaction is not None
    assert candidates[0].best_app_transaction.app_txn_id == "app-mismatch"


def test_date_mismatch_referenced_app_no_missing_in_statement():
    """date_mismatch must not also produce missing_in_statement for the same app."""
    stmt = _stmt(transaction_date=date(2024, 12, 15))
    app = _app("app-date-mis", transaction_date=date(2024, 12, 1))
    candidates = match_batch([stmt], [app])

    # Should be exactly 1 candidate (date_mismatch), not also missing_in_statement
    assert len(candidates) == 1
    assert candidates[0].issue_type == IssueType.DATE_MISMATCH
    assert candidates[0].best_app_transaction is not None
    assert candidates[0].best_app_transaction.app_txn_id == "app-date-mis"


def test_merchant_mismatch_referenced_app_no_missing_in_statement():
    """merchant_mismatch must not also produce missing_in_statement for the same app."""
    stmt = _stmt(
        merchant_raw="ShopeePay", amount=Decimal("29.90"), transaction_date=date(2024, 12, 1)
    )
    app = _app(
        "app-merch-mis",
        merchant="Lazada",
        amount=Decimal("29.90"),
        transaction_date=date(2024, 12, 1),
    )
    candidates = match_batch([stmt], [app])

    # Should be merchant_mismatch (amount ok, date ok, but merchant different)
    stmt_cand = [c for c in candidates if c.issue_type != IssueType.MISSING_IN_STATEMENT]
    assert len(stmt_cand) == 1
    assert stmt_cand[0].issue_type == IssueType.MERCHANT_MISMATCH
    # No spurious missing_in_statement
    assert IssueType.MISSING_IN_STATEMENT not in [c.issue_type for c in candidates]


def test_unrelated_app_still_missing_in_statement():
    """Truly unrelated app (different merchant/amount) still gets missing_in_statement."""
    stmt = _stmt(merchant_raw="Apple", amount=Decimal("29.90"))
    app_matched = _app("app-matched", merchant="Apple", amount=Decimal("29.90"))
    app_unrelated = _app("app-unrelated", merchant="ZaloraUnrelated", amount=Decimal("999.00"))
    candidates = match_batch([stmt], [app_matched, app_unrelated])

    missing = [c for c in candidates if c.issue_type == IssueType.MISSING_IN_STATEMENT]
    assert len(missing) == 1
    assert missing[0].best_app_transaction is not None
    assert missing[0].best_app_transaction.app_txn_id == "app-unrelated"


# ---------------------------------------------------------------------------
# 8. Build summary
# ---------------------------------------------------------------------------


def test_build_summary():
    stmt = _stmt(statement_row_reference="s1")
    app = _app("app-s1")
    candidates = match_batch([stmt], [app])
    summary = build_summary(candidates)
    assert summary.get("matched", 0) == 1


# ---------------------------------------------------------------------------
# 9. Deterministic output
# ---------------------------------------------------------------------------


def test_deterministic():
    stmt = _stmt()
    app = _app("app-det")
    c1 = match_batch([stmt], [app])
    c2 = match_batch([stmt], [app])
    assert len(c1) == len(c2)
    for a, b in zip(c1, c2):
        assert a.issue_type == b.issue_type
        assert a.candidate_id == b.candidate_id


# ---------------------------------------------------------------------------
# 10. Empty inputs
# ---------------------------------------------------------------------------


def test_empty_statements():
    candidates = match_batch([], [_app("app-empty")])
    # Should have at least one candidate (the app as missing_in_statement)
    assert len(candidates) >= 1


def test_empty_apps():
    stmt = _stmt()
    candidates = match_batch([stmt], [])
    assert len(candidates) == 1
    assert candidates[0].issue_type == IssueType.MISSING_IN_APP
