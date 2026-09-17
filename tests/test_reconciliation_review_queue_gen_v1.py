"""Tests for in-memory Review Queue Generator v1.

Covers:
  1. queue items include reason codes
  2. queue items include suggested actions
  3. summary counts are correct
  4. priority ordering is correct
  5. empty input handling
  6. matched items separated from review items
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.models import (
    AppTransaction,
    IssueType,
    StatementAmountDirection,
    StatementTransaction,
    SuggestedAction,
)
from finance_core.reconciliation.review_queue import generate_review_queue


def _stmt(**kw) -> StatementTransaction:
    defaults: dict = dict(
        transaction_date=date(2024, 12, 1),
        posted_date=None,
        merchant_raw="Apple",
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
# 1. Reason codes on queue items
# ---------------------------------------------------------------------------


def test_queue_items_include_reason_codes():
    stmt = _stmt()
    app = _app("app-001")
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    assert len(items) == 1
    assert len(items[0].reason_codes) > 0
    assert items[0].issue_type == IssueType.MATCHED


# ---------------------------------------------------------------------------
# 2. Suggested actions
# ---------------------------------------------------------------------------


def test_queue_items_include_suggested_actions():
    stmt = _stmt(merchant_raw="Giant", amount=Decimal("45.50"))
    app = _app("app-x", merchant="Different", amount=Decimal("10.00"))
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    # Should have at least one non-matched item
    review_items = [q for q in items if q.issue_type != IssueType.MATCHED]
    assert len(review_items) > 0
    for item in review_items:
        assert item.suggested_action is not None
        assert isinstance(item.suggested_action, SuggestedAction)


# ---------------------------------------------------------------------------
# 3. Summary counts
# ---------------------------------------------------------------------------


def test_summary_counts_correct():
    stmt_a = _stmt(amount=Decimal("29.90"), merchant_raw="Apple", statement_row_reference="a")
    stmt_b = _stmt(amount=Decimal("45.50"), merchant_raw="Giant", statement_row_reference="b")
    app_a = _app("app-a", merchant="Apple", amount=Decimal("29.90"))
    app_b = _app("app-b", merchant="Giant", amount=Decimal("45.50"))
    app_c = _app("app-c", merchant="Extra", amount=Decimal("99.99"))

    candidates = match_batch([stmt_a, stmt_b], [app_a, app_b, app_c])
    items, summary = generate_review_queue(candidates)

    assert summary.total_statement_transactions == 2
    assert summary.total_app_transactions == 3
    assert summary.matched_count >= 0
    assert len(summary.matched_items) == summary.matched_count


# ---------------------------------------------------------------------------
# 4. Priority ordering
# ---------------------------------------------------------------------------


def test_priority_items_sorted():
    stmt = _stmt(merchant_raw="Grab", amount=Decimal("8.50"), statement_row_reference="p1")
    app_a = _app("app-pa", merchant="Grab", amount=Decimal("8.50"))
    app_b = _app("app-pb", merchant="Grab", amount=Decimal("8.50"))

    candidates = match_batch([stmt], [app_a, app_b])
    items, summary = generate_review_queue(candidates)

    # Priority items should be sorted by priority (lowest first)
    for i in range(len(summary.priority_items) - 1):
        assert summary.priority_items[i].priority <= summary.priority_items[i + 1].priority


# ---------------------------------------------------------------------------
# 5. Empty inputs
# ---------------------------------------------------------------------------


def test_empty_candidates():
    items, summary = generate_review_queue([])
    assert len(items) == 0
    assert summary.total_statement_transactions == 0


# ---------------------------------------------------------------------------
# 6. Matched vs review separation
# ---------------------------------------------------------------------------


def test_matched_items_separated():
    stmt = _stmt()
    app = _app("app-m")
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    matched = summary.matched_items
    review = summary.review_items
    assert len(matched) + len(review) == len(items)
    for m in matched:
        assert m.issue_type == IssueType.MATCHED
    for r in review:
        assert r.issue_type != IssueType.MATCHED
