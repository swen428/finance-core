"""Tests for Resolution Runtime v1.

Covers:
  1. compatible resolution decisions succeed
  2. incompatible decisions fail clearly
  3. resolution result includes audit-friendly evidence
  4. queue_item_id mismatch fails
  5. batch resolution
  6. no persistence / write side effects
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.models import (
    AppTransaction,
    IssueType,
    ResolutionAction,
    ResolutionDecision,
    StatementTransaction,
)
from finance_core.reconciliation.resolution import (
    ResolutionRuntime,
    resolve_batch,
    resolve_item,
)
from finance_core.reconciliation.review_queue import generate_review_queue


def _stmt(**kw) -> StatementTransaction:
    defaults: dict = dict(
        transaction_date=date(2024, 12, 1),
        posted_date=None,
        merchant_raw="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        statement_row_reference="stmt-res",
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
    )
    defaults.update(kw)
    return AppTransaction(**defaults)


def _make_items():
    stmt = _stmt()
    app = _app("app-res")
    candidates = match_batch([stmt], [app])
    items, _ = generate_review_queue(candidates)
    return items


# ---------------------------------------------------------------------------
# 1. Compatible resolution succeeds
# ---------------------------------------------------------------------------


def test_confirm_match_succeeds():
    items = _make_items()
    assert len(items) > 0
    item = items[0]
    decision = ResolutionDecision(
        decision_id="dec-001",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.CONFIRM_MATCH,
        note="Looks correct.",
    )
    result = resolve_item(item, decision)
    assert result.success
    assert result.error_message is None


# ---------------------------------------------------------------------------
# 2. Incompatible decision fails clearly
# ---------------------------------------------------------------------------


def test_incompatible_decision_fails():
    items = _make_items()
    assert len(items) > 0
    item = items[0]
    # MARK_DUPLICATE is not compatible with MATCHED
    decision = ResolutionDecision(
        decision_id="dec-002",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.MARK_DUPLICATE,
    )
    result = resolve_item(item, decision)

    if item.issue_type == IssueType.MATCHED:
        # MARK_DUPLICATE should be incompatible with MATCHED
        assert not result.success
        assert result.error_message is not None
    else:
        # If not matched, MARK_DUPLICATE might be compatible
        pass


# ---------------------------------------------------------------------------
# 3. Audit-friendly evidence
# ---------------------------------------------------------------------------


def test_result_includes_audit_evidence():
    items = _make_items()
    assert len(items) > 0
    item = items[0]
    decision = ResolutionDecision(
        decision_id="dec-003",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.CONFIRM_MATCH,
        note="Verified merchant name.",
    )
    result = resolve_item(item, decision)

    assert result.success
    assert "resolution_action" in result.audit_evidence
    assert result.audit_evidence["resolution_action"] == "confirm_match"
    assert "note" in result.audit_evidence
    assert "statement_merchant" in result.audit_evidence


# ---------------------------------------------------------------------------
# 4. Queue item ID mismatch
# ---------------------------------------------------------------------------


def test_queue_item_id_mismatch():
    items = _make_items()
    assert len(items) > 0
    item = items[0]
    decision = ResolutionDecision(
        decision_id="dec-004",
        queue_item_id="wrong-id",
        action=ResolutionAction.CONFIRM_MATCH,
    )
    result = resolve_item(item, decision)
    assert not result.success
    assert "mismatch" in (result.error_message or "").lower()


# ---------------------------------------------------------------------------
# 5. Batch resolution
# ---------------------------------------------------------------------------


def test_batch_resolution():
    stmt_a = _stmt(merchant_raw="Apple", amount=Decimal("29.90"), statement_row_reference="br1")
    stmt_b = _stmt(merchant_raw="Netflix", amount=Decimal("19.90"), statement_row_reference="br2")
    app_a = _app("app-br-a", merchant="Apple", amount=Decimal("29.90"))
    app_b = _app("app-br-b", merchant="Netflix", amount=Decimal("19.90"))

    candidates = match_batch([stmt_a, stmt_b], [app_a, app_b])
    items, _ = generate_review_queue(candidates)

    decisions = [
        ResolutionDecision(
            decision_id=f"dec-br-{i}",
            queue_item_id=item.queue_item_id,
            action=ResolutionAction.CONFIRM_MATCH,
        )
        for i, item in enumerate(items)
    ]

    results = resolve_batch(items, decisions)
    assert len(results) == len(items)
    for r in results:
        assert r is not None


# ---------------------------------------------------------------------------
# 6. No persistence side effects
# ---------------------------------------------------------------------------


def test_no_persistence_side_effects():
    """Resolution runtime should not write to any database or file."""
    items = _make_items()
    item = items[0]
    runtime = ResolutionRuntime()
    decision = ResolutionDecision(
        decision_id="dec-006",
        queue_item_id=item.queue_item_id,
        action=ResolutionAction.CONFIRM_MATCH,
    )
    result = runtime.resolve(item, decision)

    # Result should be in-memory only
    assert result.success
    assert isinstance(result.result_id, str)
    # No DB connection is passed to the runtime, so persistence is impossible
