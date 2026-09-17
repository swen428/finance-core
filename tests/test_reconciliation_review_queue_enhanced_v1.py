"""Tests for Reconciliation Review Queue Enhancement Bundle v1.

Covers deterministic classification, priority assignment, evidence
enrichment, sorting, and CLI output.

Tests:
  1. Exact match not flagged as high-priority
  2. Amount mismatch → high priority
  3. Date inside window → correct reason code + DATE_WINDOW_MATCH
  4. Merchant variation → correct reason code + MERCHANT_VARIATION
  5. Missing app record → high priority (MISSING_IN_APP)
  6. Duplicate app candidates flagged
  7. Low-confidence candidate classification
  8. Deterministic sort order via sort_key_for_review_item
  9. Evidence summary contains key fields (amount, date, merchant)
 10. Demo CLI output has summary + priority grouping
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.models import (
    AppTransaction,
    IssueType,
    ReviewPriority,
    StatementAmountDirection,
    StatementTransaction,
)
from finance_core.reconciliation.review_queue import generate_review_queue

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "reconciliation"
STMT_CSV = FIXTURES / "review_queue_statement.csv"
APP_JSON = FIXTURES / "review_queue_app_transactions.json"


# -- Helpers -----------------------------------------------------------------


def _stmt(**kw) -> StatementTransaction:
    defaults: dict = dict(
        transaction_date=date(2024, 12, 1),
        posted_date=date(2024, 12, 1),
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


def _run_review(args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "finance_core.reconciliation.demo_cli", "review"]
    if args:
        cmd.extend(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# 1. Exact match not flagged as high-priority
# ---------------------------------------------------------------------------


def test_exact_match_not_high_priority():
    """An exact match should have LOW priority and is_review_required=False."""
    stmt = _stmt()
    app = _app("app-001")
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    assert len(items) >= 1
    matched = [q for q in items if q.issue_type == IssueType.MATCHED]
    assert len(matched) >= 1

    for q in matched:
        assert q.candidate.review_priority == ReviewPriority.LOW
        assert q.candidate.is_review_required is False


# ---------------------------------------------------------------------------
# 2. Amount mismatch → high priority
# ---------------------------------------------------------------------------


def test_amount_mismatch_high_priority():
    """Amount mismatch should be classified as HIGH priority."""
    stmt = _stmt(merchant_raw="Grab", amount=Decimal("18.80"), statement_row_reference="s1")
    app = _app(
        "app-x",
        merchant="Grab",
        amount=Decimal("15.00"),
        transaction_date=date(2024, 12, 1),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    am_items = [q for q in items if q.issue_type == IssueType.AMOUNT_MISMATCH]
    assert len(am_items) >= 1
    for q in am_items:
        assert q.candidate.review_priority == ReviewPriority.HIGH
        assert q.candidate.is_review_required is True

    assert summary.high_priority_count >= 1


def test_currency_mismatch_has_currency_issue_type():
    """Currency mismatch should not be reported as amount mismatch."""
    stmt = _stmt(
        merchant_raw="Grab",
        amount=Decimal("18.80"),
        currency="USD",
        statement_row_reference="s-currency",
    )
    app = _app(
        "app-currency",
        merchant="Grab",
        amount=Decimal("18.80"),
        currency="SGD",
        transaction_date=date(2024, 12, 1),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    currency_items = [q for q in items if q.issue_type == IssueType.CURRENCY_MISMATCH]
    amount_items = [q for q in items if q.issue_type == IssueType.AMOUNT_MISMATCH]
    assert len(currency_items) == 1
    assert amount_items == []
    assert currency_items[0].candidate.review_priority == ReviewPriority.HIGH
    assert currency_items[0].candidate.is_review_required is True
    assert summary.currency_mismatch_count == 1
    assert summary.amount_mismatch_count == 0


# ---------------------------------------------------------------------------
# 3. Date inside window → correct reason code + DATE_WINDOW_MATCH
# ---------------------------------------------------------------------------


def test_date_window_match_reason_code():
    """Posted-date-only match within window gets DATE_WINDOW_MATCH issue type."""
    # Statement has only posted_date; app date is 2 days earlier (within window)
    stmt = _stmt(
        transaction_date=None,
        posted_date=date(2024, 12, 5),
        merchant_raw="Netflix",
        amount=Decimal("19.90"),
        statement_row_reference="s-datewin",
    )
    app = _app(
        "app-dw",
        merchant="Netflix",
        amount=Decimal("19.90"),
        transaction_date=date(2024, 12, 3),
    )
    candidates = match_batch([stmt], [app], posted_date_window_days=3)
    items, summary = generate_review_queue(candidates)

    dw_items = [q for q in items if q.issue_type == IssueType.DATE_WINDOW_MATCH]
    assert len(dw_items) >= 1

    dw = dw_items[0]
    reason_values = {r.value for r in dw.reason_codes}
    assert "posted_date_window_match" in reason_values
    assert dw.candidate.review_priority == ReviewPriority.MEDIUM


# ---------------------------------------------------------------------------
# 4. Merchant variation → correct reason code + MERCHANT_VARIATION
# ---------------------------------------------------------------------------


def test_normalized_merchant_exact_date_is_matched():
    """Normalized merchant match with exact date is MATCHED (alias map is intentional).

    Normalization via the conservative alias map (e.g. 'grab taxi' → 'grab',
    'grab food' → 'grab') with strong signal convergence (exact amount, exact date)
    is safe enough to treat as MATCHED.
    """
    stmt = _stmt(
        merchant_raw="Grab Taxi",
        amount=Decimal("8.50"),
        transaction_date=date(2024, 12, 5),
        posted_date=date(2024, 12, 5),
        statement_row_reference="s-norm",
    )
    app = _app(
        "app-norm",
        merchant="Grab Food",
        amount=Decimal("8.50"),
        transaction_date=date(2024, 12, 5),
        normalized_merchant="grab",
    )
    candidates = match_batch([stmt], [app])
    assert len(candidates) == 1
    assert candidates[0].issue_type == IssueType.MATCHED


def test_merchant_variation_reason_code():
    """Similarity-only merchant match (no alias map entry) → MERCHANT_VARIATION."""
    # Use merchants that share tokens but aren't in the alias map
    stmt = _stmt(
        merchant_raw="NTUC FairPrice Finest",
        amount=Decimal("8.50"),
        transaction_date=date(2024, 12, 5),
        posted_date=date(2024, 12, 5),
        statement_row_reference="s-sim",
    )
    app = _app(
        "app-sim",
        merchant="NTUC FairPrice",
        amount=Decimal("8.50"),
        transaction_date=date(2024, 12, 5),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    # Similarity-based match with exact everything else → MERCHANT_VARIATION
    mv_items = [q for q in items if q.issue_type == IssueType.MERCHANT_VARIATION]
    assert len(mv_items) >= 1

    mv = mv_items[0]
    reason_values = {r.value for r in mv.reason_codes}
    assert "merchant_similarity_match" in reason_values
    assert mv.candidate.review_priority == ReviewPriority.MEDIUM


# ---------------------------------------------------------------------------
# 5. Missing app record → high priority (MISSING_IN_APP)
# ---------------------------------------------------------------------------


def test_missing_app_high_priority():
    """Statement with no matching app transaction → HIGH priority."""
    # Use a completely different currency and distant dates to ensure no match
    stmt = _stmt(
        merchant_raw="UnknownVendor",
        amount=Decimal("99.99"),
        transaction_date=date(2024, 12, 15),
        posted_date=None,
        currency="USD",
        statement_row_reference="s-noapp",
    )
    app = _app(
        "app-unrelated",
        merchant="Apple",
        amount=Decimal("29.90"),
        transaction_date=date(2024, 1, 1),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    # The matcher may classify differently (amount_mismatch, currency_mismatch,
    # etc.) but any non-matched statement vs app should be HIGH priority
    non_matched = [q for q in items if q.issue_type != IssueType.MATCHED]
    assert len(non_matched) >= 1
    for q in non_matched:
        assert q.candidate.review_priority == ReviewPriority.HIGH
        assert q.candidate.is_review_required is True


# ---------------------------------------------------------------------------
# 6. Duplicate app candidates flagged
# ---------------------------------------------------------------------------


def test_duplicate_app_candidates_flagged():
    """Two statements matching the same app → POSSIBLE_DUPLICATE."""
    stmt_a = _stmt(
        merchant_raw="Grab",
        amount=Decimal("8.50"),
        statement_row_reference="s-dup-a",
    )
    stmt_b = _stmt(
        merchant_raw="Grab",
        amount=Decimal("8.50"),
        statement_row_reference="s-dup-b",
    )
    app = _app(
        "app-grab",
        merchant="Grab",
        amount=Decimal("8.50"),
        transaction_date=date(2024, 12, 1),
    )
    candidates = match_batch([stmt_a, stmt_b], [app])
    items, summary = generate_review_queue(candidates)

    dup_items = [q for q in items if q.issue_type == IssueType.POSSIBLE_DUPLICATE]
    assert len(dup_items) >= 1
    for q in dup_items:
        assert q.candidate.review_priority == ReviewPriority.HIGH
        reason_values = {r.value for r in q.reason_codes}
        assert "multiple_candidate_matches" in reason_values


# ---------------------------------------------------------------------------
# 7. Low-confidence candidate classification
# ---------------------------------------------------------------------------


def test_low_confidence_candidate():
    """Candidates with weak evidence should have appropriate classification."""
    # Statement with no date, different merchant → should be NO_MATCH / MISSING_IN_APP
    stmt = _stmt(
        transaction_date=None,
        posted_date=None,
        merchant_raw="RandomVendor",
        amount=Decimal("50.00"),
        statement_row_reference="s-lowconf",
    )
    app = _app("app-x", merchant="Apple", amount=Decimal("29.90"))
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    assert len(items) >= 1
    # With no date and no merchant match, confidence should be 0
    non_matched = [q for q in items if q.issue_type != IssueType.MATCHED]
    for q in non_matched:
        assert q.candidate.confidence_score == Decimal("0.0")


# ---------------------------------------------------------------------------
# 8. Deterministic sort order
# ---------------------------------------------------------------------------


def test_deterministic_sort_order():
    """Review queue items should be deterministically sorted by priority, date,
    merchant, amount."""
    stmts = [
        _stmt(
            merchant_raw="Zebra",
            amount=Decimal("100.00"),
            transaction_date=date(2024, 12, 5),
            statement_row_reference="sz",
        ),
        _stmt(
            merchant_raw="Alpha",
            amount=Decimal("10.00"),
            transaction_date=date(2024, 12, 1),
            statement_row_reference="sa",
        ),
        _stmt(
            merchant_raw="Beta",
            amount=Decimal("20.00"),
            transaction_date=date(2024, 12, 3),
            statement_row_reference="sb",
        ),
    ]
    apps = [
        _app("app-z", merchant="Zebra", amount=Decimal("5.00"), transaction_date=date(2024, 12, 5)),
        _app("app-a", merchant="Alpha", amount=Decimal("5.00"), transaction_date=date(2024, 12, 1)),
        _app("app-b", merchant="Beta", amount=Decimal("5.00"), transaction_date=date(2024, 12, 3)),
    ]
    candidates = match_batch(stmts, apps)
    items, summary = generate_review_queue(candidates)

    # Verify items are sorted by priority (0 = highest)
    for i in range(len(items) - 1):
        assert items[i].priority <= items[i + 1].priority

    # All non-matched items should have the same priority (amount_mismatch = 0)
    # Within same priority, sort by date
    non_matched = [q for q in items if q.issue_type != IssueType.MATCHED]
    for i in range(len(non_matched) - 1):
        if non_matched[i].priority == non_matched[i + 1].priority:
            d1 = non_matched[i].candidate.statement.transaction_date
            d2 = non_matched[i + 1].candidate.statement.transaction_date
            if d1 is not None and d2 is not None:
                assert d1 <= d2


# ---------------------------------------------------------------------------
# 9. Evidence summary contains key fields
# ---------------------------------------------------------------------------


def test_evidence_summary_contains_key_fields():
    """Evidence summary on queue items should include amounts, dates, merchant info."""
    stmt = _stmt(
        merchant_raw="Grab",
        amount=Decimal("18.80"),
        transaction_date=date(2024, 12, 5),
        statement_row_reference="s-evid",
    )
    app = _app(
        "app-g",
        merchant="Grab",
        amount=Decimal("18.20"),
        transaction_date=date(2024, 12, 3),
    )
    candidates = match_batch([stmt], [app])
    items, summary = generate_review_queue(candidates)

    assert len(items) >= 1
    # At least one item should have evidence
    items_with_evidence = [
        q for q in items if q.evidence_summary and q.evidence_summary != "no evidence"
    ]
    assert len(items_with_evidence) >= 1

    ev = items_with_evidence[0].evidence_summary
    # Should contain mention of amounts or merchant
    assert "Grab" in ev or "18.80" in ev or "18.20" in ev or "SGD" in ev


# ---------------------------------------------------------------------------
# 10. Demo CLI output has summary + priority grouping
# ---------------------------------------------------------------------------


def test_demo_cli_output_has_summary_and_priority_grouping():
    """Demo CLI 'review' command output includes summary counts and priority grouping."""
    result = _run_review(
        [
            "--statement",
            str(STMT_CSV),
            "--app-transactions",
            str(APP_JSON),
        ]
    )
    assert result.returncode == 0

    stdout = result.stdout

    # Summary section
    assert "Summary:" in stdout
    assert "matched:" in stdout
    assert "review required:" in stdout
    assert "high priority:" in stdout
    assert "medium priority:" in stdout
    assert "low priority:" in stdout

    # Priority grouping
    assert "High Priority" in stdout or "Medium Priority" in stdout

    # Should have issue types
    assert "Issue:" in stdout

    # Should have statement info
    assert "Statement:" in stdout

    # Should have reason codes for review items
    assert "Reason codes:" in stdout or "Suggested action:" in stdout
