"""Read-only review summary layer for persisted guarded reconciliation
apply execution results.

This module adds a deterministic, read-only review summary that makes
PR #100's persisted execution results easier to inspect, test, and
present to a human reviewer.  It never mutates final financial records.

Key invariants:
- Read-only: never creates, updates, or deletes final financial records.
- Deterministic: same input always produces the same summary.
- No settlement obligation generation.
- No Telegram / OCR / PDF / Metabase interaction.
- No write to ``database/finance.db``.
- No migration required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from finance_core.reconciliation.models import (
    ApplyExecutionStatus,
    GuardedApplyExecutionResult,
    GuardedOperationResult,
    ReviewPriority,
)

if TYPE_CHECKING:
    from finance_core.reconciliation.apply_runtime_persistence import (
        GuardedApplyExecutionRepository,
    )

# ---------------------------------------------------------------------------
# Review summary dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardedApplyExecutionReviewSummary:
    """Deterministic read-only review summary for a single
    ``GuardedApplyExecutionResult``.

    Built by ``build_guarded_apply_execution_review_summary()``.
    All fields are derived from the source execution result; none are
    random, time-dependent, or AI-generated.
    """

    plan_id: str
    idempotency_key: str
    execution_status: ApplyExecutionStatus
    total_operations: int
    operations_executed: int
    operations_blocked: int
    operations_skipped: int
    approved_operation_count: int
    blocked_operation_count: int
    unsupported_operation_count: int
    conflict_operation_count: int
    mutation_types: tuple[str, ...]
    guard_decision_refs: tuple[str, ...]
    is_dry_run: bool
    executed_at: str
    block_reason: str
    has_blocking_outcome: bool
    requires_human_review: bool
    review_priority: ReviewPriority
    human_summary: str


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


def build_guarded_apply_execution_review_summary(
    result: GuardedApplyExecutionResult,
) -> GuardedApplyExecutionReviewSummary:
    """Build a deterministic read-only review summary from a guarded apply
    execution result.

    The summary is purely derived from the input. It does not read any
    database, generate timestamps, or use randomness.
    """
    operations_executed = _count_status(result.results, ApplyExecutionStatus.EXECUTED)
    operations_blocked = _count_status(result.results, ApplyExecutionStatus.BLOCKED)
    operations_skipped = len(result.results) - operations_executed - operations_blocked

    approved_operation_count = sum(1 for r in result.results if r.guard_decision_approved)
    blocked_operation_count = sum(1 for r in result.results if not r.guard_decision_approved)
    unsupported_operation_count = _count_status(result.results, ApplyExecutionStatus.UNSUPPORTED)
    conflict_operation_count = _count_status(result.results, ApplyExecutionStatus.CONFLICT)

    mutation_types = _collect_mutation_types(result.results)
    has_blocking = _has_blocking_outcome(result)
    requires_review = _requires_human_review(result)
    priority = _derive_review_priority(result)
    summary_text = _build_human_summary(
        result.execution_status,
        operations_executed,
        operations_blocked,
        operations_skipped,
        result.is_dry_run,
    )

    return GuardedApplyExecutionReviewSummary(
        plan_id=result.plan_id,
        idempotency_key=result.idempotency_key,
        execution_status=result.execution_status,
        total_operations=result.total_operations,
        operations_executed=operations_executed,
        operations_blocked=operations_blocked,
        operations_skipped=operations_skipped,
        approved_operation_count=approved_operation_count,
        blocked_operation_count=blocked_operation_count,
        unsupported_operation_count=unsupported_operation_count,
        conflict_operation_count=conflict_operation_count,
        mutation_types=mutation_types,
        guard_decision_refs=result.guard_decision_refs,
        is_dry_run=result.is_dry_run,
        executed_at=result.executed_at,
        block_reason=result.block_reason,
        has_blocking_outcome=has_blocking,
        requires_human_review=requires_review,
        review_priority=priority,
        human_summary=summary_text,
    )


def summarize_guarded_apply_execution_from_repository(
    repo: GuardedApplyExecutionRepository,
    *,
    idempotency_key: str | None = None,
    execution_id: str | None = None,
) -> GuardedApplyExecutionReviewSummary | None:
    """Load a persisted execution result from the repository and build a
    review summary.

    Exactly one of ``idempotency_key`` or ``execution_id`` must be
    supplied. Returns ``None`` when no matching execution exists.
    """
    if idempotency_key is not None and execution_id is not None:
        raise ValueError("Supply exactly one of idempotency_key or execution_id, not both.")
    if idempotency_key is None and execution_id is None:
        raise ValueError("Supply exactly one of idempotency_key or execution_id.")

    if idempotency_key is not None:
        result = repo.get_by_idempotency_key(idempotency_key)
    else:
        # execution_id is not None because of the guard above
        result = repo.get_by_execution_id(execution_id)  # type: ignore[arg-type]

    if result is None:
        return None

    return build_guarded_apply_execution_review_summary(result)


def list_guarded_apply_execution_review_summaries(
    repo: GuardedApplyExecutionRepository,
    *,
    execution_status: ApplyExecutionStatus | None = None,
    requires_human_review: bool | None = None,
    limit: int | None = None,
) -> tuple[GuardedApplyExecutionReviewSummary, ...]:
    """List persisted guarded execution results as read-only review summaries.

    The operator ordering is deterministic:
    high-priority summaries first, then medium, then low; within each
    priority group, newest ``executed_at`` first and ``idempotency_key`` as
    the stable tie-breaker.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative.")

    results = repo.list_execution_results(execution_status=execution_status)
    summaries = [build_guarded_apply_execution_review_summary(result) for result in results]

    if requires_human_review is not None:
        summaries = [
            summary
            for summary in summaries
            if summary.requires_human_review is requires_human_review
        ]

    summaries.sort(key=lambda summary: summary.idempotency_key)
    summaries.sort(key=lambda summary: summary.executed_at, reverse=True)
    summaries.sort(key=lambda summary: _review_priority_rank(summary.review_priority))

    if limit is not None:
        summaries = summaries[:limit]

    return tuple(summaries)


# ---------------------------------------------------------------------------
# Internal helpers -- deterministic
# ---------------------------------------------------------------------------


def _count_status(
    results: tuple[GuardedOperationResult, ...],
    status: ApplyExecutionStatus,
) -> int:
    return sum(1 for r in results if r.execution_status == status)


def _collect_mutation_types(
    results: tuple[GuardedOperationResult, ...],
) -> tuple[str, ...]:
    """Collect and sort mutation types from operation results.

    Empty strings are excluded and the result is sorted for determinism.
    """
    types = sorted({r.mutation_type for r in results if r.mutation_type})
    return tuple(types)


def _has_blocking_outcome(result: GuardedApplyExecutionResult) -> bool:
    """Return True when the execution result represents a blocking or
    partial-blocking outcome requiring attention."""
    return result.execution_status in (
        ApplyExecutionStatus.BLOCKED,
        ApplyExecutionStatus.PARTIALLY_BLOCKED,
        ApplyExecutionStatus.CONFLICT,
    )


def _requires_human_review(result: GuardedApplyExecutionResult) -> bool:
    """Return True when the execution result should be flagged for human
    review.

    Rules:
    - BLOCKED, PARTIALLY_BLOCKED, or CONFLICT status always requires review.
    - UNSUPPORTED operations that would otherwise go unnoticed require review.
    """
    if _has_blocking_outcome(result):
        return True
    if result.execution_status == ApplyExecutionStatus.UNSUPPORTED:
        return True
    # Check individual operation statuses for unsupported
    for r in result.results:
        if r.execution_status in (
            ApplyExecutionStatus.UNSUPPORTED,
            ApplyExecutionStatus.BLOCKED,
            ApplyExecutionStatus.CONFLICT,
            ApplyExecutionStatus.PARTIALLY_BLOCKED,
        ):
            return True
    return False


def _derive_review_priority(
    result: GuardedApplyExecutionResult,
) -> ReviewPriority:
    """Derive a deterministic review priority from the execution result.

    Rules:
    - **high**: execution_status is BLOCKED, PARTIALLY_BLOCKED, or CONFLICT,
      or any operation has blocked/conflict status.
    - **medium**: unsupported operations exist, or execution is dry-run with
      non-empty mutation payloads.
    - **low**: all operations executed and no blocked/conflict/unsupported
      statuses.
    """
    if _has_blocking_outcome(result):
        return ReviewPriority.HIGH

    # Plan-level UNSUPPORTED status (e.g. empty plan)
    if result.execution_status == ApplyExecutionStatus.UNSUPPORTED:
        return ReviewPriority.MEDIUM

    for r in result.results:
        if r.execution_status in (
            ApplyExecutionStatus.BLOCKED,
            ApplyExecutionStatus.PARTIALLY_BLOCKED,
            ApplyExecutionStatus.CONFLICT,
        ):
            return ReviewPriority.HIGH

    has_unsupported = any(
        r.execution_status == ApplyExecutionStatus.UNSUPPORTED for r in result.results
    )
    if has_unsupported:
        return ReviewPriority.MEDIUM

    # Dry-run with non-empty mutation payloads gets medium priority
    # so reviewers can inspect the proposed mutations.
    if result.is_dry_run and result.results:
        return ReviewPriority.MEDIUM

    return ReviewPriority.LOW


def _review_priority_rank(priority: ReviewPriority) -> int:
    """Sort HIGH before MEDIUM before LOW for operator review queues."""
    if priority == ReviewPriority.HIGH:
        return 0
    if priority == ReviewPriority.MEDIUM:
        return 1
    return 2


def _build_human_summary(
    status: ApplyExecutionStatus,
    executed: int,
    blocked: int,
    skipped: int,
    is_dry_run: bool,
) -> str:
    """Build a concise, deterministic human-readable summary string."""
    if status == ApplyExecutionStatus.BLOCKED:
        return f"Execution blocked: {executed} executed, {blocked} blocked, {skipped} skipped."
    if status == ApplyExecutionStatus.PARTIALLY_BLOCKED:
        return (
            f"Execution partially blocked: {executed} executed, "
            f"{blocked} blocked, {skipped} skipped."
        )
    if status == ApplyExecutionStatus.CONFLICT:
        return f"Execution conflict: {executed} executed, {blocked} blocked, {skipped} skipped."
    if status == ApplyExecutionStatus.UNSUPPORTED:
        return f"Execution unsupported: {executed} executed, {blocked} blocked, {skipped} skipped."
    if is_dry_run:
        return (
            f"Execution completed as dry-run: {executed} operations "
            f"reviewed, {executed} would execute."
        )
    return f"Execution completed: {executed} executed, {blocked} blocked, {skipped} skipped."


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------

__all__ = [
    "GuardedApplyExecutionReviewSummary",
    "build_guarded_apply_execution_review_summary",
    "list_guarded_apply_execution_review_summaries",
    "summarize_guarded_apply_execution_from_repository",
]
