"""Tests for Resolution Persistence v1.

Covers:
  1. Persist resolution decisions
  2. Persist resolution results
  3. Compatible successful resolution updates queue status to resolved
  4. Ignore updates queue status to ignored
  5. needs_more_info updates queue status to needs_more_info
  6. Failed incompatible resolution keeps queue status pending
  7. audit_evidence_json is stored and reloadable
  8. No app transaction mutation occurs
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from finance_core.reconciliation.matching import match_batch
from finance_core.reconciliation.models import (
    AppTransaction,
    IssueType,
    ResolutionAction,
    ResolutionDecision,
    StatementAmountDirection,
    StatementTransaction,
)
from finance_core.reconciliation.persistence import apply_reconciliation_review_schema
from finance_core.reconciliation.resolution import ResolutionRuntime
from finance_core.reconciliation.resolution_persistence import (
    DuplicateResolutionPersistenceError,
    ResolutionPersistence,
)
from finance_core.reconciliation.review_persistence import ReviewQueuePersistence
from finance_core.reconciliation.review_queue import generate_review_queue

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_stmt(**kw) -> StatementTransaction:
    defaults = dict(
        transaction_date=date(2024, 12, 1),
        posted_date=None,
        merchant_raw="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        statement_row_reference="r-res",
        amount_direction=StatementAmountDirection.DEBIT,
        raw_amount="29.90",
    )
    defaults.update(kw)
    return StatementTransaction(**defaults)


def _make_app(app_txn_id: str, **kw) -> AppTransaction:
    defaults = dict(
        app_txn_id=app_txn_id,
        transaction_date=date(2024, 12, 1),
        merchant="Apple",
        amount=Decimal("29.90"),
        currency="SGD",
        source_type="expense",
    )
    defaults.update(kw)
    return AppTransaction(**defaults)


def _setup_db() -> tuple[sqlite3.Connection, ReviewQueuePersistence, ResolutionPersistence]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_reconciliation_review_schema(conn)
    rqp = ReviewQueuePersistence(conn)
    rp = ResolutionPersistence(conn)
    return conn, rqp, rp


def _persist_matched_item(rqp: ReviewQueuePersistence) -> str:
    """Persist a matched review queue item and return its queue_item_id."""
    stmt = _make_stmt()
    app = _make_app("app-res-persist")
    candidates = match_batch([stmt], [app])
    items, _ = generate_review_queue(candidates)
    rqp.persist_review_queue(items)
    return items[0].queue_item_id


# ---------------------------------------------------------------------------
# 1. Persist resolution decisions
# ---------------------------------------------------------------------------


def test_persist_decision():
    conn, rqp, rp = _setup_db()
    try:
        qid = _persist_matched_item(rqp)

        decision = ResolutionDecision(
            decision_id="dec-001",
            queue_item_id=qid,
            action=ResolutionAction.CONFIRM_MATCH,
            note="Looks correct.",
        )
        rp.persist_decision(decision)

        rows = rp.list_decisions_for_queue_item(qid)
        assert len(rows) == 1
        assert rows[0]["public_id"] == "dec-001"
        assert rows[0]["decision_action"] == "confirm_match"
        assert rows[0]["decision_note"] == "Looks correct."
    finally:
        conn.close()


def test_persist_decision_idempotent():
    """Duplicate decision public_id should be ignored."""
    conn, rqp, rp = _setup_db()
    try:
        qid = _persist_matched_item(rqp)

        decision = ResolutionDecision(
            decision_id="dec-dup",
            queue_item_id=qid,
            action=ResolutionAction.CONFIRM_MATCH,
        )
        rp.persist_decision(decision)
        rp.persist_decision(decision)

        rows = rp.list_decisions_for_queue_item(qid)
        assert len(rows) == 1
    finally:
        conn.close()


def test_conflicting_duplicate_decision_id_is_rejected_without_status_drift():
    """A reused decision_id must not update queue status without matching audit rows."""
    conn, rqp, rp = _setup_db()
    try:
        qid = _persist_matched_item(rqp)
        item = _row_to_queue_item(rqp, qid)

        first = ResolutionDecision(
            decision_id="dec-conflict",
            queue_item_id=qid,
            action=ResolutionAction.CONFIRM_MATCH,
        )
        first_result = rp.apply_resolution(item, first)
        assert first_result.success
        assert rqp.get_by_public_id(qid)["status"] == "resolved"

        conflicting = ResolutionDecision(
            decision_id="dec-conflict",
            queue_item_id=qid,
            action=ResolutionAction.IGNORE,
        )
        with pytest.raises(DuplicateResolutionPersistenceError):
            rp.apply_resolution(item, conflicting)

        assert rqp.get_by_public_id(qid)["status"] == "resolved"
        decisions = rp.list_decisions_for_queue_item(qid)
        results = rp.list_results_for_queue_item(qid)
        assert len(decisions) == 1
        assert len(results) == 1
        assert decisions[0]["decision_action"] == "confirm_match"
        audit = json.loads(results[0]["audit_evidence_json"])
        assert audit["resolution_action"] == "confirm_match"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Persist resolution results
# ---------------------------------------------------------------------------


def test_persist_result():
    conn, rqp, rp = _setup_db()
    try:
        qid = _persist_matched_item(rqp)

        item = _row_to_queue_item(rqp, qid)

        decision = ResolutionDecision(
            decision_id="dec-res-01",
            queue_item_id=qid,
            action=ResolutionAction.CONFIRM_MATCH,
        )
        runtime = ResolutionRuntime()
        result = runtime.resolve(item, decision)

        rp.persist_decision(decision)
        rp.persist_result(result)

        result_rows = rp.list_results_for_queue_item(qid)
        assert len(result_rows) == 1
        assert result_rows[0]["success"] == 1
        assert result_rows[0]["public_id"] == result.result_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. Compatible successful resolution updates queue status to resolved
# ---------------------------------------------------------------------------


def test_confirm_match_updates_status_to_resolved():
    conn, rqp, rp = _setup_db()
    try:
        qid = _persist_matched_item(rqp)
        item = _row_to_queue_item(rqp, qid)

        decision = ResolutionDecision(
            decision_id="dec-resolved",
            queue_item_id=qid,
            action=ResolutionAction.CONFIRM_MATCH,
            note="Confirmed.",
        )
        result = rp.apply_resolution(item, decision)

        assert result.success
        row = rqp.get_by_public_id(qid)
        assert row["status"] == "resolved"
    finally:
        conn.close()


def test_adjust_app_transaction_updates_status_to_resolved():
    conn, rqp, rp = _setup_db()
    try:
        # Create an amount_mismatch item
        stmt = _make_stmt(amount=Decimal("29.90"))
        app = _make_app("app-amt-mis", amount=Decimal("35.00"))
        candidates = match_batch([stmt], [app])
        items, _ = generate_review_queue(candidates)
        rqp.persist_review_queue(items)
        # Find the amount_mismatch item
        qid = None
        for item in items:
            if item.issue_type == IssueType.AMOUNT_MISMATCH:
                qid = item.queue_item_id
                break
        assert qid is not None

        item = _row_to_queue_item(rqp, qid)
        decision = ResolutionDecision(
            decision_id="dec-adjust",
            queue_item_id=qid,
            action=ResolutionAction.ADJUST_APP_TRANSACTION,
        )
        result = rp.apply_resolution(item, decision)
        assert result.success
        row = rqp.get_by_public_id(qid)
        assert row["status"] == "resolved"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. IGNORE updates queue status to ignored
# ---------------------------------------------------------------------------


def test_ignore_updates_status_to_ignored():
    conn, rqp, rp = _setup_db()
    try:
        qid = _persist_matched_item(rqp)
        item = _row_to_queue_item(rqp, qid)

        decision = ResolutionDecision(
            decision_id="dec-ignore",
            queue_item_id=qid,
            action=ResolutionAction.IGNORE,
            note="Not important.",
        )
        result = rp.apply_resolution(item, decision)

        assert result.success
        row = rqp.get_by_public_id(qid)
        assert row["status"] == "ignored"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. NEEDS_MORE_INFO updates queue status to needs_more_info
# ---------------------------------------------------------------------------


def test_needs_more_info_updates_status():
    conn, rqp, rp = _setup_db()
    try:
        qid = _persist_matched_item(rqp)
        item = _row_to_queue_item(rqp, qid)

        decision = ResolutionDecision(
            decision_id="dec-nmi",
            queue_item_id=qid,
            action=ResolutionAction.NEEDS_MORE_INFO,
            note="Need to check with the vendor.",
        )
        result = rp.apply_resolution(item, decision)

        assert result.success
        row = rqp.get_by_public_id(qid)
        assert row["status"] == "needs_more_info"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. Failed incompatible resolution keeps queue status pending
# ---------------------------------------------------------------------------


def test_incompatible_resolution_keeps_pending():
    conn, rqp, rp = _setup_db()
    try:
        qid = _persist_matched_item(rqp)
        item = _row_to_queue_item(rqp, qid)

        # MARK_DUPLICATE is incompatible with IssueType.MATCHED
        decision = ResolutionDecision(
            decision_id="dec-incompat",
            queue_item_id=qid,
            action=ResolutionAction.MARK_DUPLICATE,
        )
        result = rp.apply_resolution(item, decision)

        if item.issue_type == IssueType.MATCHED:
            assert not result.success
            row = rqp.get_by_public_id(qid)
            assert row["status"] == "pending"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7. audit_evidence_json is stored and reloadable
# ---------------------------------------------------------------------------


def test_audit_evidence_stored_and_reloadable():
    conn, rqp, rp = _setup_db()
    try:
        qid = _persist_matched_item(rqp)
        item = _row_to_queue_item(rqp, qid)

        decision = ResolutionDecision(
            decision_id="dec-audit",
            queue_item_id=qid,
            action=ResolutionAction.CONFIRM_MATCH,
            note="Audit test.",
        )
        result = rp.apply_resolution(item, decision)
        assert result.success

        result_rows = rp.list_results_for_queue_item(qid)
        assert len(result_rows) == 1

        audit_json = result_rows[0]["audit_evidence_json"]
        audit = json.loads(audit_json)
        assert "resolution_action" in audit
        assert audit["resolution_action"] == "confirm_match"
        assert "note" in audit
        assert "statement_merchant" in audit
    finally:
        conn.close()


def test_successful_duplicate_persists_every_app_target() -> None:
    conn, rqp, rp = _setup_db()
    try:
        stmt = _make_stmt(merchant_raw="Grab")
        apps = [_make_app("app-dup-a", merchant="Grab"), _make_app("app-dup-b", merchant="Grab")]
        items, _ = generate_review_queue(match_batch([stmt], apps))
        item = next(item for item in items if item.issue_type == IssueType.POSSIBLE_DUPLICATE)
        rqp.persist_review_queue(items)
        decision = ResolutionDecision(
            decision_id="dec-complete-dup",
            queue_item_id=item.queue_item_id,
            action=ResolutionAction.MARK_DUPLICATE,
        )
        result = rp.apply_resolution(item, decision)
        assert result.success
        row = rp.list_results_for_queue_item(item.queue_item_id)[0]
        evidence = json.loads(row["audit_evidence_json"])
        assert set(evidence["duplicate_app_txn_ids"]) == {"app-dup-a", "app-dup-b"}
        assert evidence["kept_app_txn_id"] == item.candidate.best_app_transaction.app_txn_id
        assert evidence["audit_only"] is True
        assert evidence["canonical_app_transaction_ref"] == evidence["app_txn_id"]
        assert evidence["payload_target_app_txn_id"] == evidence["app_txn_id"]
    finally:
        conn.close()


def test_successful_resolution_replay_refuses_corrected_target_under_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, rqp, rp = _setup_db()
    try:
        qid = _persist_matched_item(rqp)
        item = _row_to_queue_item(rqp, qid)
        decision = ResolutionDecision(
            decision_id="dec-corrected-target",
            queue_item_id=qid,
            action=ResolutionAction.CONFIRM_MATCH,
        )
        rp.apply_resolution(item, decision)
        monkeypatch.setattr(
            "finance_core.reconciliation.resolution_persistence.has_committed_correction",
            lambda _conn, target: target == "app-res-persist",
        )
        monkeypatch.setattr(
            "finance_core.reconciliation.resolution_persistence.verify_correction_schema",
            lambda _conn: True,
        )
        with pytest.raises(
            ValueError, match="corrected_transaction_requires_versioned_reconciliation"
        ):
            rp.apply_resolution(item, decision)
        with pytest.raises(
            ValueError, match="corrected_transaction_requires_versioned_reconciliation"
        ):
            rp.persist_decision(decision)
        assert conn.in_transaction is False
        assert len(rp.list_results_for_queue_item(qid)) == 1
    finally:
        conn.close()


def test_resolution_caller_transaction_is_not_committed() -> None:
    conn, rqp, rp = _setup_db()
    try:
        qid = _persist_matched_item(rqp)
        conn.execute("CREATE TABLE caller_marker (value TEXT)")
        conn.execute("BEGIN")
        conn.execute("INSERT INTO caller_marker VALUES ('pending')")
        decision = ResolutionDecision(
            decision_id="dec-caller-owned",
            queue_item_id=qid,
            action=ResolutionAction.CONFIRM_MATCH,
        )
        with pytest.raises(RuntimeError, match="caller-owned"):
            rp.persist_decision(decision)
        assert conn.in_transaction is True
        assert conn.execute("SELECT value FROM caller_marker").fetchone()[0] == "pending"
        conn.rollback()
        assert conn.execute("SELECT 1 FROM caller_marker").fetchone() is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 8. No app transaction mutation
# ---------------------------------------------------------------------------


def test_no_app_transaction_mutation():
    """Verify that apply_resolution does not modify the AppTransaction object."""
    conn, rqp, rp = _setup_db()
    try:
        qid = _persist_matched_item(rqp)

        # Preserve original values from the row
        row = rqp.get_by_public_id(qid)
        original_app_ref = row["app_transaction_ref"]

        item = _row_to_queue_item(rqp, qid)
        decision = ResolutionDecision(
            decision_id="dec-no-mutate",
            queue_item_id=qid,
            action=ResolutionAction.CONFIRM_MATCH,
        )
        result = rp.apply_resolution(item, decision)
        assert result.success

        # Re-read and verify app_transaction_ref is unchanged
        updated_row = rqp.get_by_public_id(qid)
        assert updated_row["app_transaction_ref"] == original_app_ref
    finally:
        conn.close()


def test_resolution_audit_foreign_keys_reject_orphans():
    conn, _rqp, _rp = _setup_db()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO reconciliation_resolution_decisions (
                    public_id, review_queue_public_id, decision_action, reviewer
                ) VALUES ('orphan-decision', 'missing-review-item', 'ignore', 'human')
                """
            )

        qid = _persist_matched_item(_rqp)
        decision = ResolutionDecision(
            decision_id="dec-fk",
            queue_item_id=qid,
            action=ResolutionAction.CONFIRM_MATCH,
        )
        _rp.persist_decision(decision)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO reconciliation_resolution_results (
                    public_id, review_queue_public_id, decision_public_id,
                    success, audit_evidence_json
                ) VALUES (
                    'orphan-result', ?, 'missing-decision', 1, '{}'
                )
                """,
                (qid,),
            )
    finally:
        conn.close()


def test_apply_resolution_rolls_back_decision_when_result_conflicts():
    conn, rqp, rp = _setup_db()
    try:
        qid = _persist_matched_item(rqp)
        item = _row_to_queue_item(rqp, qid)

        existing_decision = ResolutionDecision(
            decision_id="dec-existing-result-owner",
            queue_item_id=qid,
            action=ResolutionAction.CONFIRM_MATCH,
        )
        rp.persist_decision(existing_decision)
        conn.execute(
            """
            INSERT INTO reconciliation_resolution_results (
                public_id, review_queue_public_id, decision_public_id,
                success, audit_evidence_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "res-dec-atomic-conflict",
                qid,
                existing_decision.decision_id,
                1,
                '{"preexisting":true}',
            ),
        )
        conn.commit()

        conflicting_decision = ResolutionDecision(
            decision_id="dec-atomic-conflict",
            queue_item_id=qid,
            action=ResolutionAction.CONFIRM_MATCH,
        )
        with pytest.raises(DuplicateResolutionPersistenceError):
            rp.apply_resolution(item, conflicting_decision)

        assert rqp.get_by_public_id(qid)["status"] == "pending"
        rolled_back_decision = conn.execute(
            """
            SELECT 1
            FROM reconciliation_resolution_decisions
            WHERE public_id = ?
            """,
            (conflicting_decision.decision_id,),
        ).fetchone()
        assert rolled_back_decision is None
        assert len(rp.list_results_for_queue_item(qid)) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_queue_item(rqp, queue_item_id):  # type: ignore[assignment]
    """Reconstruct a ReviewQueueItem from a persisted row."""
    from finance_core.reconciliation.models import (
        AppTransaction,
        IssueType,
        MatchStatus,
        ReasonCode,
        ReconciliationCandidate,
        ReviewQueueItem,
        StatementTransaction,
        SuggestedAction,
    )

    row = rqp.get_by_public_id(queue_item_id)
    assert row is not None

    evidence = json.loads(row["evidence_json"] or "{}")
    reason_codes_raw = json.loads(row["reason_codes_json"] or "[]")
    reason_codes = tuple(ReasonCode(r) for r in reason_codes_raw)

    stmt = StatementTransaction(
        transaction_date=None,
        posted_date=None,
        merchant_raw=evidence.get("statement_merchant", ""),
        amount=Decimal(evidence["statement_amount"]) if evidence.get("statement_amount") else None,
        currency=evidence.get("statement_currency"),
    )

    app = None
    if evidence.get("app_txn_id"):
        app = AppTransaction(
            app_txn_id=evidence["app_txn_id"],
            transaction_date=date(2024, 12, 1),  # reconstructed default
            merchant=evidence.get("app_merchant", ""),
            amount=Decimal(evidence.get("app_amount", "0")),
            currency=evidence.get("app_currency", ""),
        )

    candidate = ReconciliationCandidate(
        statement=stmt,
        best_app_transaction=app,
        match_status=MatchStatus("matched"),
        reason_codes=reason_codes,
        issue_type=IssueType(row["issue_type"]),
        confidence_score=Decimal(row["confidence_score"] or "0"),
        candidate_id=row["candidate_id"],
    )

    item = _load_review_item(rqp, queue_item_id)
    if item is not None:
        return item

    return ReviewQueueItem(
        queue_item_id=row["public_id"],
        candidate=candidate,
        issue_type=IssueType(row["issue_type"]),
        suggested_action=SuggestedAction(row["suggested_action"]),
        reason_codes=reason_codes,
        evidence_summary="reconstructed",
        priority=row["priority"],
    )


def _load_review_item(rqp, qid):
    """Load a ReviewQueueItem from persisted data using the full reconstruction."""
    from finance_core.reconciliation.models import (
        AppTransaction,
        IssueType,
        MatchStatus,
        ReasonCode,
        ReconciliationCandidate,
        ReviewQueueItem,
        StatementTransaction,
        SuggestedAction,
    )

    row = rqp.get_by_public_id(qid)
    if row is None:
        return None

    evidence = json.loads(row["evidence_json"] or "{}")
    reason_codes_raw = json.loads(row["reason_codes_json"] or "[]")
    reason_codes = tuple(
        ReasonCode(r) if r in {rc.value for rc in ReasonCode} else ReasonCode.NEEDS_REVIEW
        for r in reason_codes_raw
    )

    stmt = StatementTransaction(
        transaction_date=None,
        posted_date=None,
        merchant_raw=evidence.get("statement_merchant", ""),
        amount=Decimal(evidence["statement_amount"]) if evidence.get("statement_amount") else None,
        currency=evidence.get("statement_currency"),
        statement_row_reference=row["statement_transaction_ref"],
    )

    app = None
    if evidence.get("app_txn_id"):
        app = AppTransaction(
            app_txn_id=evidence["app_txn_id"],
            transaction_date=date(2024, 12, 1),
            merchant=evidence.get("app_merchant", ""),
            amount=Decimal(evidence.get("app_amount", "0")),
            currency=evidence.get("app_currency", ""),
        )

    candidate = ReconciliationCandidate(
        statement=stmt,
        best_app_transaction=app,
        match_status=MatchStatus("matched"),
        reason_codes=reason_codes,
        issue_type=IssueType(row["issue_type"]),
        confidence_score=Decimal(row["confidence_score"] or "0"),
        candidate_id=row["candidate_id"],
    )

    return ReviewQueueItem(
        queue_item_id=row["public_id"],
        candidate=candidate,
        issue_type=IssueType(row["issue_type"]),
        suggested_action=SuggestedAction(row["suggested_action"]),
        reason_codes=reason_codes,
        evidence_summary=f"reconstructed from persisted row {row['public_id']}",
        priority=row["priority"],
    )
