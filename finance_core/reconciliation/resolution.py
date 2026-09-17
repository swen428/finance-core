"""Reconciliation Review Queue v1 -- resolution runtime.

Applies human ``ResolutionDecision`` objects to ``ReviewQueueItem`` objects
and returns deterministic, audit-friendly ``ResolutionResult`` objects.

Key design properties:
- In-memory only: no database writes in v1.
- Validates action/issue-type compatibility before resolving.
- Deterministic: same inputs always produce the same output.
- Audit-friendly: every result carries structured evidence.
- Designed for future SQLite persistence adapter.

Supported resolution actions:
- ``confirm_match``: accept the matched candidate.
- ``adjust_app_transaction``: flag for app-side adjustment.
- ``create_missing_app_transaction``: flag to create a missing app record.
- ``mark_statement_only``: record as statement-only entry.
- ``mark_duplicate``: resolve a duplicate.
- ``ignore``: dismiss the item.
- ``needs_more_info``: escalate for further investigation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from finance_core.reconciliation.models import (
    ResolutionDecision,
    ResolutionResult,
    ReviewQueueItem,
    validate_resolution_decision,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class ResolutionRuntime:
    """Stateless resolution runtime that applies decisions to queue items.

    Usage::

        runtime = ResolutionRuntime()
        decision = ResolutionDecision(
            decision_id="dec-001",
            queue_item_id="q-000",
            action=ResolutionAction.CONFIRM_MATCH,
            note="Looks correct.",
        )
        result = runtime.resolve(item, decision)
        assert result.success
    """

    def resolve(
        self,
        item: ReviewQueueItem,
        decision: ResolutionDecision,
    ) -> ResolutionResult:
        """Apply a resolution decision to a review queue item.

        Validates that (a) the decision's ``queue_item_id`` matches the
        item, and (b) the resolution action is compatible with the item's
        issue type.

        Parameters
        ----------
        item:
            The review queue item to resolve.
        decision:
            The human reviewer's decision.

        Returns
        -------
        ResolutionResult
            Deterministic outcome with audit evidence.
        """
        # Guard: queue_item_id must match
        if decision.queue_item_id != item.queue_item_id:
            return ResolutionResult(
                result_id=_make_result_id(item, decision),
                decision=decision,
                queue_item=item,
                success=False,
                audit_evidence={
                    "validation_error": (
                        f"Decision queue_item_id '{decision.queue_item_id}' "
                        f"does not match item queue_item_id '{item.queue_item_id}'"
                    ),
                },
                error_message="queue_item_id mismatch",
            )

        # Guard: resolution action must be compatible with issue type
        is_compat, error = validate_resolution_decision(decision.action, item.issue_type)
        if not is_compat:
            return ResolutionResult(
                result_id=_make_result_id(item, decision),
                decision=decision,
                queue_item=item,
                success=False,
                audit_evidence={
                    "validation_error": error or "incompatible resolution action",
                    "action": decision.action.value,
                    "issue_type": item.issue_type.value,
                },
                error_message=error,
            )

        # -- Build success result --
        now_iso = datetime.now(timezone.utc).isoformat()
        evidence: dict[str, Any] = {
            "resolution_action": decision.action.value,
            "issue_type": item.issue_type.value,
            "resolved_at": decision.resolved_at or now_iso,
            "reviewer": decision.reviewer,
            "note": decision.note,
            "queue_item_id": item.queue_item_id,
            "candidate_id": item.candidate.candidate_id,
            "confidence_score": str(item.candidate.confidence_score),
            "reason_codes": [r.value for r in item.reason_codes],
            "evidence_summary": item.evidence_summary,
        }

        # Add statement/merchant evidence from the candidate
        cand = item.candidate
        evidence["statement_merchant"] = cand.statement.merchant_raw
        evidence["statement_amount"] = (
            str(cand.statement.amount) if cand.statement.amount is not None else None
        )
        evidence["statement_currency"] = cand.statement.currency
        if cand.statement.transaction_date:
            evidence["statement_txn_date"] = cand.statement.transaction_date.isoformat()
        if cand.statement.posted_date:
            evidence["statement_posted_date"] = cand.statement.posted_date.isoformat()

        if cand.best_app_transaction is not None:
            app = cand.best_app_transaction
            evidence["app_txn_id"] = app.app_txn_id
            evidence["app_merchant"] = app.merchant
            evidence["app_amount"] = str(app.amount)
            evidence["app_currency"] = app.currency
            evidence["app_txn_date"] = app.transaction_date.isoformat()

        return ResolutionResult(
            result_id=_make_result_id(item, decision),
            decision=decision,
            queue_item=item,
            success=True,
            audit_evidence=evidence,
            error_message=None,
        )


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

_DEFAULT_RUNTIME = ResolutionRuntime()


def resolve_item(
    item: ReviewQueueItem,
    decision: ResolutionDecision,
) -> ResolutionResult:
    """Convenience function to resolve a single item using the default runtime."""
    return _DEFAULT_RUNTIME.resolve(item, decision)


def resolve_batch(
    items: list[ReviewQueueItem],
    decisions: list[ResolutionDecision],
) -> list[ResolutionResult]:
    """Resolve multiple queue items against a list of decisions.

    Matches decisions to items by ``queue_item_id``.  Items without a
    matching decision are not resolved.
    """
    decision_map: dict[str, ResolutionDecision] = {d.queue_item_id: d for d in decisions}
    results: list[ResolutionResult] = []
    for item in items:
        decision = decision_map.get(item.queue_item_id)
        if decision is not None:
            results.append(resolve_item(item, decision))
    return results


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _make_result_id(item: ReviewQueueItem, decision: ResolutionDecision) -> str:
    """Create a stable result identifier."""
    return f"res-{decision.decision_id}"


__all__ = [
    "ResolutionRuntime",
    "resolve_item",
    "resolve_batch",
]
