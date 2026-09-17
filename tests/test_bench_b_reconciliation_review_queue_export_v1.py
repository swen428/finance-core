"""Tests for Bench B -- Reconciliation Review Queue Export v1.

Covers the read-only, deterministic export projection in
``finance_core.reconciliation/review_queue_export.py``.

Coverage:
  1. Empty input returns an empty export.
  2. Blocked guarded apply review summary appears in the export.
  3. Partially blocked guarded apply review summary requires human review.
  4. Priority / status counts are correct.
  5. Deterministic sorting (highest priority first, then status, then date,
     then review_id).
  6. Reason codes are preserved.
  7. Evidence references are preserved.
  8. ``generated_at`` is injectable for deterministic tests.
  9. Monetary ``Decimal`` values are not converted through ``float``.
 10. The export path does not call apply / final mutation behavior (read-only).
 11. ``ReviewQueueItem`` sources preserve merchant/date/amount/currency.
 12. Raw / unsupported inputs are rejected (type-guarded boundary).
 13. ``ReviewQueueEntry``-like persisted views are accepted.
 14. ``database/finance.db`` is untouched.
 15. Public API is exported from ``finance_core.reconciliation``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from finance_core.reconciliation import (
    ReviewQueueExport,
    ReviewQueueExportItem,
    build_review_queue_export,
)
from finance_core.reconciliation.apply_execution_review import (
    GuardedApplyExecutionReviewSummary,
    build_guarded_apply_execution_review_summary,
)
from finance_core.reconciliation.models import (
    ApplyExecutionStatus,
    AppTransaction,
    GuardedApplyExecutionResult,
    GuardedOperationResult,
    IssueType,
    MatchStatus,
    ReasonCode,
    ReconciliationCandidate,
    ReconciliationReviewEvidence,
    ReviewPriority,
    ReviewQueueItem,
    StatementTransaction,
    SuggestedAction,
)
from finance_core.reconciliation.review_queue import ReviewQueueEntry
from tests.conftest import LIVE_DB_PATH

FIXED_TS = "2026-06-20T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixtures -- guarded apply execution results / summaries
# ---------------------------------------------------------------------------


def _executed_result() -> GuardedApplyExecutionResult:
    return GuardedApplyExecutionResult(
        plan_id="plan-exec",
        idempotency_key="idem-exec",
        execution_status=ApplyExecutionStatus.EXECUTED,
        results=(
            GuardedOperationResult(
                operation_id="op-exec",
                decision_id="dec-exec",
                execution_status=ApplyExecutionStatus.EXECUTED,
                guard_decision_approved=True,
                mutation_type="noop",
            ),
        ),
        total_operations=1,
        operated_executed=1,
        operated_blocked=0,
        guard_decision_refs=("gdec-exec",),
        is_dry_run=True,
    )


def _blocked_result() -> GuardedApplyExecutionResult:
    return GuardedApplyExecutionResult(
        plan_id="plan-block",
        idempotency_key="idem-block",
        execution_status=ApplyExecutionStatus.BLOCKED,
        results=(
            GuardedOperationResult(
                operation_id="op-block",
                decision_id="dec-block",
                execution_status=ApplyExecutionStatus.BLOCKED,
                guard_decision_approved=False,
                guard_decision_idempotency_key="gdec-block",
                guard_blocked_reasons=("missing_amount",),
                mutation_type="create_final_transaction_proposal",
            ),
        ),
        total_operations=1,
        operated_executed=0,
        operated_blocked=1,
        block_reason="guard_denied",
        guard_decision_refs=("gdec-block",),
        is_dry_run=True,
    )


def _partially_blocked_result() -> GuardedApplyExecutionResult:
    return GuardedApplyExecutionResult(
        plan_id="plan-pb",
        idempotency_key="idem-pb",
        execution_status=ApplyExecutionStatus.PARTIALLY_BLOCKED,
        results=(
            GuardedOperationResult(
                operation_id="op-ok",
                decision_id="dec-ok",
                execution_status=ApplyExecutionStatus.EXECUTED,
                guard_decision_approved=True,
                mutation_type="noop",
            ),
            GuardedOperationResult(
                operation_id="op-block",
                decision_id="dec-block",
                execution_status=ApplyExecutionStatus.BLOCKED,
                guard_decision_approved=False,
                guard_decision_idempotency_key="gdec-pb",
                guard_blocked_reasons=("missing_amount",),
                mutation_type="create_final_transaction_proposal",
            ),
        ),
        total_operations=2,
        operated_executed=1,
        operated_blocked=1,
        block_reason="",
        guard_decision_refs=("gdec-pb",),
        is_dry_run=True,
    )


def _executed_summary() -> GuardedApplyExecutionReviewSummary:
    return build_guarded_apply_execution_review_summary(_executed_result())


def _blocked_summary() -> GuardedApplyExecutionReviewSummary:
    return build_guarded_apply_execution_review_summary(_blocked_result())


def _partially_blocked_summary() -> GuardedApplyExecutionReviewSummary:
    return build_guarded_apply_execution_review_summary(_partially_blocked_result())


# ---------------------------------------------------------------------------
# Fixtures -- review queue items (candidate-enriched, carry merchant/date/...)
# ---------------------------------------------------------------------------


def _statement(
    *,
    merchant: str = "Giant Supermarket",
    txn_date: date | None = date(2024, 12, 1),
    amount: Decimal = Decimal("45.50"),
    currency: str = "SGD",
    row_ref: str | None = "stmt-row-001",
) -> StatementTransaction:
    return StatementTransaction(
        transaction_date=txn_date,
        posted_date=date(2024, 12, 2),
        merchant_raw=merchant,
        amount=amount,
        currency=currency,
        statement_row_reference=row_ref,
    )


def _app_txn(*, app_id: str = "app-txn-001") -> AppTransaction:
    return AppTransaction(
        app_txn_id=app_id,
        transaction_date=date(2024, 12, 1),
        merchant="Giant Supermarket",
        amount=Decimal("45.50"),
        currency="SGD",
    )


def _amount_mismatch_item(
    *,
    queue_id: str = "q-amt",
    candidate_id: str = "cand-amt",
    priority: ReviewPriority = ReviewPriority.HIGH,
    txn_date_override: date | None = None,
    missing_txn_date: bool = False,
) -> ReviewQueueItem:
    if missing_txn_date:
        stmt = _statement(txn_date=None)
    else:
        stmt = _statement(txn_date=txn_date_override) if txn_date_override else _statement()
    cand = ReconciliationCandidate(
        statement=stmt,
        best_app_transaction=_app_txn(),
        match_status=MatchStatus.AMOUNT_MISMATCH,
        candidate_id=candidate_id,
        review_priority=priority,
        issue_type=IssueType.AMOUNT_MISMATCH,
        reason_codes=(ReasonCode.AMOUNT_DIFFERS,),
    )
    evidence = ReconciliationReviewEvidence(
        statement_reference="stmt-row-001",
        app_transaction_id="app-txn-001",
        statement_amount=stmt.amount,
        app_amount=Decimal("44.00"),
        amount_delta=Decimal("1.50"),
        reason_codes=(ReasonCode.AMOUNT_DIFFERS,),
        issue_type=IssueType.AMOUNT_MISMATCH,
        review_priority=ReviewPriority.HIGH,
    )
    return ReviewQueueItem(
        candidate=cand,
        issue_type=IssueType.AMOUNT_MISMATCH,
        suggested_action=SuggestedAction.ADJUST_APP_TRANSACTION,
        queue_item_id=queue_id,
        reason_codes=(ReasonCode.AMOUNT_DIFFERS,),
        structured_evidence=evidence,
    )


def _matched_item(
    *,
    queue_id: str = "q-match",
    candidate_id: str = "cand-match",
    txn_date: date = date(2024, 12, 1),
) -> ReviewQueueItem:
    cand = ReconciliationCandidate(
        statement=_statement(txn_date=txn_date),
        best_app_transaction=_app_txn(),
        match_status=MatchStatus.MATCHED,
        candidate_id=candidate_id,
        review_priority=ReviewPriority.LOW,
        issue_type=IssueType.MATCHED,
        reason_codes=(ReasonCode.EXACT_AMOUNT_MATCH,),
    )
    return ReviewQueueItem(
        candidate=cand,
        issue_type=IssueType.MATCHED,
        suggested_action=SuggestedAction.CONFIRM_MATCH,
        queue_item_id=queue_id,
        reason_codes=(ReasonCode.EXACT_AMOUNT_MATCH,),
    )


def _needs_review_item(
    *,
    queue_id: str = "q-review",
    candidate_id: str = "cand-review",
) -> ReviewQueueItem:
    cand = ReconciliationCandidate(
        statement=_statement(merchant="Foo Bar"),
        best_app_transaction=None,
        match_status=MatchStatus.NEEDS_REVIEW,
        candidate_id=candidate_id,
        review_priority=ReviewPriority.MEDIUM,
        issue_type=IssueType.NEEDS_REVIEW,
        reason_codes=(ReasonCode.NEEDS_REVIEW,),
    )
    return ReviewQueueItem(
        candidate=cand,
        issue_type=IssueType.NEEDS_REVIEW,
        suggested_action=SuggestedAction.NEEDS_MORE_INFO,
        queue_item_id=queue_id,
        reason_codes=(ReasonCode.NEEDS_REVIEW,),
    )


def _review_queue_entry(
    *,
    public_id: str = "rq-000042",
    match_status: str = "amount_mismatch",
    reason_codes: list[str] | None = None,
    internal_candidate_id: str | None = "icand-001",
    amount_delta: Decimal | None = Decimal("1.50"),
) -> ReviewQueueEntry:
    return ReviewQueueEntry(
        id=1,
        public_id=public_id,
        run_id=1,
        run_public_id="run-0001",
        statement_transaction_id=10,
        match_status=match_status,
        reason_codes=reason_codes if reason_codes is not None else ["amount_differs"],
        evidence={"matching_decision": "ref-001", "review_resolution": "ref-002"},
        internal_candidate_id=internal_candidate_id,
        amount_delta=amount_delta,
        date_delta_days=0,
        merchant_similarity=0.9,
        created_at="2026-06-20T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Test 1: Empty input
# ---------------------------------------------------------------------------


class TestEmptyInput:
    def test_empty_input_returns_empty_export(self):
        export = build_review_queue_export([])
        assert isinstance(export, ReviewQueueExport)
        assert export.total_items == 0
        assert export.items == ()
        assert export.by_priority == {"high": 0, "medium": 0, "low": 0}
        assert export.by_status == {}

    def test_empty_input_generated_at_defaults_to_empty(self):
        export = build_review_queue_export([])
        assert export.generated_at == ""

    def test_empty_input_generated_at_injectable(self):
        export = build_review_queue_export([], generated_at=FIXED_TS)
        assert export.generated_at == FIXED_TS


# ---------------------------------------------------------------------------
# Test 2: Blocked item appears in export
# ---------------------------------------------------------------------------


class TestBlockedItemAppears:
    def test_blocked_summary_produces_export_item(self):
        export = build_review_queue_export([_blocked_summary()])
        assert export.total_items == 1
        item = export.items[0]
        assert item.review_id == "idem-block"
        assert item.status == "blocked"
        assert item.priority == ReviewPriority.HIGH
        assert item.requires_human_review is True
        assert item.source_type == "guarded_apply_review_summary"

    def test_blocked_reason_code_preserved(self):
        export = build_review_queue_export([_blocked_summary()])
        item = export.items[0]
        # block_reason is surfaced as a reason code for guarded apply summaries
        assert "guard_denied" in item.reason_codes

    def test_blocked_guard_decision_refs_preserved_as_evidence(self):
        export = build_review_queue_export([_blocked_summary()])
        item = export.items[0]
        assert item.evidence_refs == ("gdec-block",)


# ---------------------------------------------------------------------------
# Test 3: Partially blocked requires human review
# ---------------------------------------------------------------------------


class TestPartiallyBlockedRequiresHumanReview:
    def test_partially_blocked_requires_human_review(self):
        export = build_review_queue_export([_partially_blocked_summary()])
        assert export.total_items == 1
        item = export.items[0]
        assert item.requires_human_review is True
        assert item.priority == ReviewPriority.HIGH
        assert item.status == "partially_blocked"

    def test_partially_blocked_guard_refs_preserved(self):
        export = build_review_queue_export([_partially_blocked_summary()])
        item = export.items[0]
        assert item.evidence_refs == ("gdec-pb",)


# ---------------------------------------------------------------------------
# Test 4: Priority / status counts
# ---------------------------------------------------------------------------


class TestCounts:
    def test_priority_counts_for_mixed_summaries(self):
        export = build_review_queue_export(
            [_blocked_summary(), _executed_summary(), _partially_blocked_summary()]
        )
        assert export.by_priority == {"high": 2, "medium": 1, "low": 0}

    def test_status_counts_for_mixed_summaries(self):
        export = build_review_queue_export(
            [_blocked_summary(), _executed_summary(), _partially_blocked_summary()]
        )
        # by_status keys are the stable ApplyExecutionStatus values
        assert export.by_status == {
            "blocked": 1,
            "executed": 1,
            "partially_blocked": 1,
        }

    def test_total_items_equals_input_length(self):
        export = build_review_queue_export([_blocked_summary(), _executed_summary()])
        assert export.total_items == 2
        assert len(export.items) == 2

    def test_review_queue_item_priority_counts(self):
        export = build_review_queue_export(
            [_amount_mismatch_item(), _matched_item(), _needs_review_item()]
        )
        assert export.by_priority == {"high": 1, "medium": 1, "low": 1}

    def test_review_queue_item_status_counts(self):
        export = build_review_queue_export(
            [_amount_mismatch_item(), _matched_item(), _needs_review_item()]
        )
        assert export.by_status == {
            "amount_mismatch": 1,
            "matched": 1,
            "needs_review": 1,
        }


# ---------------------------------------------------------------------------
# Test 5: Deterministic sorting
# ---------------------------------------------------------------------------


class TestDeterministicSorting:
    def test_highest_priority_first(self):
        # low, medium, high supplied out of order
        export = build_review_queue_export(
            [_matched_item(), _needs_review_item(), _amount_mismatch_item()]
        )
        priorities = [item.priority for item in export.items]
        assert priorities == [ReviewPriority.HIGH, ReviewPriority.MEDIUM, ReviewPriority.LOW]

    def test_status_breaks_priority_tie(self):
        # Two HIGH-priority guarded summaries with different statuses
        export = build_review_queue_export([_partially_blocked_summary(), _blocked_summary()])
        # blocked < partially_blocked lexicographically
        statuses = [item.status for item in export.items]
        assert statuses == ["blocked", "partially_blocked"]

    def test_transaction_date_breaks_status_tie(self):
        # Two review queue items with same priority + status, different dates
        earlier = _amount_mismatch_item(queue_id="q-earlier", txn_date_override=date(2024, 12, 1))
        later = _amount_mismatch_item(queue_id="q-later", txn_date_override=date(2024, 12, 5))
        # Supply in reverse so sorting is actually tested
        export = build_review_queue_export([later, earlier])
        review_ids = [item.review_id for item in export.items]
        assert review_ids == ["q-earlier", "q-later"]

    def test_missing_transaction_date_sorts_after_dated_items(self):
        # Same priority + status: dated records are more actionable than thin
        # or incomplete records, so missing dates sort last.
        no_date = _amount_mismatch_item(queue_id="q-no-date", missing_txn_date=True)
        with_date = _amount_mismatch_item(queue_id="q-with-date")
        export = build_review_queue_export([no_date, with_date])
        assert [item.review_id for item in export.items] == ["q-with-date", "q-no-date"]

    def test_review_id_breaks_date_tie(self):
        # Same priority, status, and date -> review_id ascending tie-break
        a = _amount_mismatch_item(queue_id="q-zeta")
        b = _amount_mismatch_item(queue_id="q-alpha")
        export = build_review_queue_export([a, b])
        assert [item.review_id for item in export.items] == ["q-alpha", "q-zeta"]

    def test_sorting_is_deterministic_across_calls(self):
        sources = [
            _blocked_summary(),
            _matched_item(),
            _amount_mismatch_item(),
            _needs_review_item(),
            _executed_summary(),
        ]
        e1 = build_review_queue_export(sources)
        e2 = build_review_queue_export(list(reversed(sources)))
        assert e1.items == e2.items
        assert e1 == e2


# ---------------------------------------------------------------------------
# Test 6: Reason codes preserved
# ---------------------------------------------------------------------------


class TestReasonCodesPreserved:
    def test_structured_evidence_reason_codes_preserved(self):
        export = build_review_queue_export([_amount_mismatch_item()])
        item = export.items[0]
        assert item.reason_codes == ("amount_differs",)

    def test_candidate_reason_codes_fallback_when_no_structured_evidence(self):
        export = build_review_queue_export([_matched_item()])
        item = export.items[0]
        assert item.reason_codes == ("exact_amount_match",)

    def test_review_queue_entry_reason_codes_preserved(self):
        export = build_review_queue_export([_review_queue_entry()])
        item = export.items[0]
        assert item.reason_codes == ("amount_differs",)


# ---------------------------------------------------------------------------
# Test 7: Evidence references preserved
# ---------------------------------------------------------------------------


class TestEvidenceRefsPreserved:
    def test_structured_evidence_refs_preserved(self):
        export = build_review_queue_export([_amount_mismatch_item()])
        item = export.items[0]
        # statement reference, app transaction id, and app_txn ref are included
        assert "stmt-row-001" in item.evidence_refs
        assert "app-txn-001" in item.evidence_refs
        assert "app_txn:app-txn-001" in item.evidence_refs

    def test_guard_decision_refs_are_evidence_refs(self):
        export = build_review_queue_export([_blocked_summary()])
        item = export.items[0]
        assert item.evidence_refs == ("gdec-block",)

    def test_review_queue_entry_evidence_refs_from_dict_keys(self):
        export = build_review_queue_export([_review_queue_entry()])
        item = export.items[0]
        # evidence dict keys are sorted + deduplicated; internal candidate id appended
        assert "matching_decision" in item.evidence_refs
        assert "review_resolution" in item.evidence_refs
        assert "icand-001" in item.evidence_refs


# ---------------------------------------------------------------------------
# Test 8: generated_at injectable
# ---------------------------------------------------------------------------


class TestGeneratedAtInjectable:
    def test_generated_at_default_empty(self):
        export = build_review_queue_export([_blocked_summary()])
        assert export.generated_at == ""

    def test_generated_at_injected_verbatim(self):
        export = build_review_queue_export([_blocked_summary()], generated_at=FIXED_TS)
        assert export.generated_at == FIXED_TS

    def test_generated_at_does_not_affect_items(self):
        e1 = build_review_queue_export([_blocked_summary()], generated_at="")
        e2 = build_review_queue_export([_blocked_summary()], generated_at=FIXED_TS)
        assert e1.items == e2.items
        assert e1.by_priority == e2.by_priority
        assert e1.by_status == e2.by_status


# ---------------------------------------------------------------------------
# Test 9: Decimal not converted through float
# ---------------------------------------------------------------------------


class TestDecimalSafety:
    def test_amount_stays_decimal_in_dataclass(self):
        export = build_review_queue_export([_amount_mismatch_item()])
        item = export.items[0]
        assert isinstance(item.amount, Decimal)
        assert item.amount == Decimal("45.50")

    def test_to_dict_serializes_decimal_as_string(self):
        export = build_review_queue_export([_amount_mismatch_item()])
        item = export.items[0]
        payload = item.to_dict()
        assert isinstance(payload["amount"], str)
        assert payload["amount"] == "45.50"

    def test_to_dict_does_not_produce_float(self):
        export = build_review_queue_export([_amount_mismatch_item()])
        payload = export.items[0].to_dict()
        # No field should be a float for monetary values
        for key in ("amount", "amount_delta"):
            value = payload.get(key)
            if value is not None:
                assert not isinstance(value, float), f"{key} should not be float"

    def test_review_queue_entry_amount_delta_stays_decimal(self):
        export = build_review_queue_export([_review_queue_entry()])
        item = export.items[0]
        assert isinstance(item.amount_delta, Decimal)
        assert item.amount_delta == Decimal("1.50")

    def test_decimal_precision_preserved(self):
        # A high-precision decimal that would lose precision through float
        precise = Decimal("0.10000000000000001")
        stmt = _statement(amount=precise)
        cand = ReconciliationCandidate(
            statement=stmt,
            match_status=MatchStatus.AMOUNT_MISMATCH,
            candidate_id="cand-precise",
            review_priority=ReviewPriority.HIGH,
            issue_type=IssueType.AMOUNT_MISMATCH,
        )
        item = ReviewQueueItem(
            candidate=cand,
            issue_type=IssueType.AMOUNT_MISMATCH,
            suggested_action=SuggestedAction.ADJUST_APP_TRANSACTION,
            queue_item_id="q-precise",
        )
        export = build_review_queue_export([item])
        row = export.items[0]
        assert row.amount == precise
        assert row.to_dict()["amount"] == str(precise)


# ---------------------------------------------------------------------------
# Test 10: Export path is read-only (no apply / final mutation behavior)
# ---------------------------------------------------------------------------


class TestReadOnlyBoundary:
    def test_does_not_open_or_write_finance_db(self):
        # Patch sqlite3.connect so any accidental DB access would be caught.
        with patch.object(sqlite3, "connect", side_effect=AssertionError("DB touched")):
            export = build_review_queue_export([_blocked_summary(), _amount_mismatch_item()])
        assert export.total_items == 2

    def test_does_not_import_apply_or_final_mutation_runtimes(self):
        # The builder must not trigger apply execution / final mutation code.
        # Guard by ensuring the apply / final mutation entry points are never
        # called during a build.
        import finance_core.reconciliation.apply as apply_mod
        import finance_core.reconciliation.final_mutation_workflow as fmw

        with patch.object(apply_mod, "apply_decisions", side_effect=AssertionError):
            with patch.object(
                fmw, "execute_guarded_final_mutation_workflow", side_effect=AssertionError
            ):
                export = build_review_queue_export([_blocked_summary(), _amount_mismatch_item()])
        assert export.total_items == 2

    def test_export_item_is_frozen(self):
        export = build_review_queue_export([_blocked_summary()])
        item = export.items[0]
        with pytest.raises(FrozenInstanceError):
            item.review_id = "mutated"  # type: ignore[misc]

    def test_export_is_frozen(self):
        export = build_review_queue_export([_blocked_summary()])
        with pytest.raises(FrozenInstanceError):
            export.total_items = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 11: ReviewQueueItem sources preserve merchant/date/amount/currency
# ---------------------------------------------------------------------------


class TestReviewQueueItemRichFields:
    def test_merchant_date_amount_currency_preserved(self):
        export = build_review_queue_export([_amount_mismatch_item()])
        item = export.items[0]
        assert item.merchant == "Giant Supermarket"
        assert item.transaction_date == date(2024, 12, 1)
        assert item.amount == Decimal("45.50")
        assert item.currency == "SGD"

    def test_statement_and_app_transaction_ids_preserved(self):
        export = build_review_queue_export([_amount_mismatch_item()])
        item = export.items[0]
        assert item.statement_transaction_id == "stmt-row-001"
        assert item.app_transaction_id == "app-txn-001"

    def test_matched_item_requires_no_human_review(self):
        export = build_review_queue_export([_matched_item()])
        item = export.items[0]
        assert item.requires_human_review is False
        assert item.priority == ReviewPriority.LOW


# ---------------------------------------------------------------------------
# Test 12: Type-guarded input boundary
# ---------------------------------------------------------------------------


class TestTypeGuardedBoundary:
    def test_raw_dict_rejected(self):
        with pytest.raises(TypeError):
            build_review_queue_export([{"review_id": "x"}])  # type: ignore[list-item]

    def test_string_rejected(self):
        with pytest.raises(TypeError):
            build_review_queue_export(["not-a-source"])  # type: ignore[list-item]

    def test_none_rejected(self):
        with pytest.raises(TypeError):
            build_review_queue_export([None])  # type: ignore[list-item]

    def test_int_rejected(self):
        with pytest.raises(TypeError):
            build_review_queue_export([42])  # type: ignore[list-item]

    def test_lookalike_missing_evidence_rejected(self):
        # Has public_id / match_status / reason_codes / internal_candidate_id /
        # amount_delta but is missing ``evidence`` -> must be rejected.
        class MissingEvidence:
            public_id = "rq-x"
            match_status = "matched"
            reason_codes: list[str] = []
            internal_candidate_id = None
            amount_delta = None

        with pytest.raises(TypeError):
            build_review_queue_export([MissingEvidence()])  # type: ignore[list-item]

    def test_lookalike_missing_amount_delta_rejected(self):
        # Has the other required attrs but is missing ``amount_delta``.
        class MissingAmountDelta:
            public_id = "rq-x"
            match_status = "matched"
            reason_codes: list[str] = []
            evidence: dict[str, object] = {}
            internal_candidate_id = None

        with pytest.raises(TypeError):
            build_review_queue_export([MissingAmountDelta()])  # type: ignore[list-item]

    def test_lookalike_missing_internal_candidate_id_rejected(self):
        # Has the other required attrs but is missing ``internal_candidate_id``.
        class MissingInternalCandidate:
            public_id = "rq-x"
            match_status = "matched"
            reason_codes: list[str] = []
            evidence: dict[str, object] = {}
            amount_delta = None

        with pytest.raises(TypeError):
            build_review_queue_export([MissingInternalCandidate()])  # type: ignore[list-item]

    def test_minimal_valid_lookalike_accepted(self):
        # An object exposing exactly the required attribute set (with sensible
        # values) must still be accepted and projected as a review queue entry.
        class MinimalLookalike:
            public_id = "rq-min"
            match_status = "amount_mismatch"
            reason_codes = ["amount_differs"]
            evidence = {"matching_decision": "ref-1"}
            internal_candidate_id = "icand-min"
            amount_delta = Decimal("2.00")
            # Optional attrs the adapter reads defensively:
            statement_transaction_id = 77

        export = build_review_queue_export([MinimalLookalike()])  # type: ignore[list-item]
        assert export.total_items == 1
        item = export.items[0]
        assert item.review_id == "rq-min"
        assert item.source_type == "review_queue_entry"
        assert item.internal_candidate_id == "icand-min"
        assert item.amount_delta == Decimal("2.00")
        assert item.statement_transaction_id == "77"

    def test_lookalike_with_only_three_old_attrs_rejected(self):
        # The pre-cleanup check accepted public_id + match_status + reason_codes
        # alone. After tightening, that is no longer enough.
        class OldThreeAttrsOnly:
            public_id = "rq-x"
            match_status = "matched"
            reason_codes: list[str] = []

        with pytest.raises(TypeError):
            build_review_queue_export([OldThreeAttrsOnly()])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Test 13: ReviewQueueEntry-like persisted views accepted
# ---------------------------------------------------------------------------


class TestReviewQueueEntrySource:
    def test_entry_projected_to_export_item(self):
        export = build_review_queue_export([_review_queue_entry()])
        assert export.total_items == 1
        item = export.items[0]
        assert item.review_id == "rq-000042"
        assert item.status == "amount_mismatch"
        assert item.priority == ReviewPriority.HIGH
        assert item.requires_human_review is True
        assert item.source_type == "review_queue_entry"
        assert item.internal_candidate_id == "icand-001"
        assert item.amount_delta == Decimal("1.50")

    def test_entry_matched_status_is_low_priority(self):
        entry = _review_queue_entry(match_status="matched", reason_codes=["exact_amount_match"])
        export = build_review_queue_export([entry])
        item = export.items[0]
        assert item.priority == ReviewPriority.LOW
        assert item.requires_human_review is False

    def test_mixed_sources_sorted_together(self):
        export = build_review_queue_export(
            [
                _matched_item(),  # LOW
                _review_queue_entry(),  # HIGH
                _needs_review_item(),  # MEDIUM
            ]
        )
        priorities = [item.priority for item in export.items]
        assert priorities == [ReviewPriority.HIGH, ReviewPriority.MEDIUM, ReviewPriority.LOW]


# ---------------------------------------------------------------------------
# Test 14: database/finance.db untouched
# ---------------------------------------------------------------------------


class TestLiveDbNotTouched:
    def test_build_does_not_reference_live_db_path(self):
        # The builder is pure and takes only in-memory objects; assert the
        # export module never imports sqlite3 or performs file I/O on the live
        # database. (The docstring legitimately mentions ``database/finance.db``
        # as a non-goal, so we check for actual DB access primitives instead.)
        module_path = (
            Path(__file__).resolve().parents[1]
            / "finance_core"
            / "reconciliation"
            / "review_queue_export.py"
        )
        source = module_path.read_text(encoding="utf-8")
        assert "import sqlite3" not in source
        assert "sqlite3.connect" not in source
        assert ".execute(" not in source
        assert "open(" not in source

    def test_export_works_without_any_db(self):
        # No sqlite3 connection is needed to build an export.
        export = build_review_queue_export(
            [_blocked_summary(), _amount_mismatch_item(), _review_queue_entry()]
        )
        assert export.total_items == 3
        assert str(LIVE_DB_PATH) not in repr(export)


# ---------------------------------------------------------------------------
# Test 15: Public API exported
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_names_exported_from_package(self):
        import finance_core.reconciliation as pkg

        assert hasattr(pkg, "ReviewQueueExport")
        assert hasattr(pkg, "ReviewQueueExportItem")
        assert hasattr(pkg, "build_review_queue_export")
        assert "ReviewQueueExport" in pkg.__all__
        assert "ReviewQueueExportItem" in pkg.__all__
        assert "build_review_queue_export" in pkg.__all__

    def test_build_returns_typed_export(self):
        export = build_review_queue_export([_blocked_summary()])
        assert isinstance(export, ReviewQueueExport)
        assert all(isinstance(i, ReviewQueueExportItem) for i in export.items)


# ---------------------------------------------------------------------------
# to_dict serialization
# ---------------------------------------------------------------------------


class TestToDict:
    def test_to_dict_is_deterministic(self):
        item = _amount_mismatch_item()
        d1 = build_review_queue_export([item]).items[0].to_dict()
        d2 = build_review_queue_export([item]).items[0].to_dict()
        assert d1 == d2

    def test_to_dict_date_serialized_as_iso(self):
        export = build_review_queue_export([_amount_mismatch_item()])
        payload = export.items[0].to_dict()
        assert payload["transaction_date"] == "2024-12-01"

    def test_to_dict_priority_serialized_as_value(self):
        export = build_review_queue_export([_amount_mismatch_item()])
        payload = export.items[0].to_dict()
        assert payload["priority"] == "high"

    def test_to_dict_preserves_field_order(self):
        from dataclasses import fields

        export = build_review_queue_export([_amount_mismatch_item()])
        payload = export.items[0].to_dict()
        declared = [f.name for f in fields(ReviewQueueExportItem)]
        assert list(payload.keys()) == declared


# ---------------------------------------------------------------------------
# Determinism / equality
# ---------------------------------------------------------------------------


class TestDeterminismAndEquality:
    def test_same_inputs_same_export(self):
        sources = [_blocked_summary(), _amount_mismatch_item(), _matched_item()]
        e1 = build_review_queue_export(sources, generated_at=FIXED_TS)
        e2 = build_review_queue_export(sources, generated_at=FIXED_TS)
        assert e1 == e2

    def test_different_generated_at_breaks_equality(self):
        sources = [_blocked_summary()]
        e1 = build_review_queue_export(sources, generated_at="a")
        e2 = build_review_queue_export(sources, generated_at="b")
        assert e1 != e2


# Local helper to build an amount-mismatch item with a specific transaction
# date is defined above (``_amount_mismatch_item`` accepts ``txn_date_override``).
