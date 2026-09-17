"""Read-only review queue report for guarded reconciliation apply
execution results requiring human review.

This module builds on the existing guarded apply execution persistence
and review summary layer to produce a deterministic, sortable review
queue. It is purely read-only and never mutates final financial records.

Key invariants:
- Read-only: never creates, updates, or deletes final financial records.
- Deterministic: same input always produces the same queue.
- No settlement obligation generation.
- No Telegram / OCR / PDF / Metabase interaction.
- No write to ``database/finance.db``.
- No migration required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from finance_core.reconciliation.models import (
    ApplyExecutionStatus,
    GuardedApplyExecutionResult,
    ReviewPriority,
)

if TYPE_CHECKING:
    from finance_core.reconciliation.apply_runtime_persistence import (
        GuardedApplyExecutionRepository,
    )

# ---------------------------------------------------------------------------
# Review queue entry dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyReviewQueueEntry:
    """A single entry in the reconciliation apply execution review queue.

    Built by ``build_apply_execution_review_queue()`` from persisted
    ``GuardedApplyExecutionResult`` data. All fields are derived from
    existing data structures — none are random, time-dependent, or
    AI-generated.
    """

    execution_id: str
    idempotency_key: str
    plan_id: str
    execution_status: ApplyExecutionStatus
    review_priority: ReviewPriority
    requires_human_review: bool
    is_blocking: bool
    is_partially_blocked: bool
    operations_executed: int
    operations_blocked: int
    operations_skipped: int
    total_operations: int
    block_reason: str
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    human_summary: str = ""
    executed_at: str = ""
    is_dry_run: bool = True
    guard_decision_refs: tuple[str, ...] = field(default_factory=tuple)
    mutation_types: tuple[str, ...] = field(default_factory=tuple)
    operation_refs: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_apply_execution_review_queue(
    repo: GuardedApplyExecutionRepository,
    *,
    requires_human_review: bool | None = None,
    execution_status: ApplyExecutionStatus | None = None,
    limit: int | None = None,
) -> tuple[ApplyReviewQueueEntry, ...]:
    """Build a deterministic read-only review queue from persisted guarded
    apply execution results.

    Parameters
    ----------
    repo:
        A ``GuardedApplyExecutionRepository`` connected to a SQLite
        database with migration 014 applied.
    requires_human_review:
        When ``True``, return only entries that require human review.
        When ``False``, return only entries that do not.
        When ``None`` (default), return all entries.
    execution_status:
        Optional filter on ``ApplyExecutionStatus``, applied before
        queue entry building.
    limit:
        Optional cap on the number of entries returned after sorting.
        Must be non-negative.

    Returns
    -------
    tuple of ``ApplyReviewQueueEntry``, deterministically sorted.

    Sorting rules (deterministic):
    1. Highest review priority first (HIGH → MEDIUM → LOW).
    2. Blocking or partially-blocked entries before non-blocking
       entries within the same priority tier.
    3. Newest ``executed_at`` first.
    4. ``execution_id`` ascending as the stable tie-breaker.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative.")

    results = repo.list_execution_results(execution_status=execution_status)

    entries: list[ApplyReviewQueueEntry] = []
    for result in results:
        entry = _build_entry_from_result(result)
        entries.append(entry)

    if requires_human_review is not None:
        entries = [e for e in entries if e.requires_human_review is requires_human_review]

    # Deterministic sort:
    #   1. review_priority rank (HIGH=0, MEDIUM=1, LOW=2) ASC
    #   2. blocking entries before non-blocking (False=0, True=1 → False sorts first,
    #      so we negate: -(is_blocking or is_partially_blocked))
    #   3. executed_at DESC (newest first)
    #   4. execution_id ASC (stable tie-breaker)
    entries.sort(key=lambda e: e.execution_id)
    entries.sort(key=lambda e: e.executed_at, reverse=True)
    entries.sort(key=lambda e: 0 if (e.is_blocking or e.is_partially_blocked) else 1)
    entries.sort(key=lambda e: _review_priority_rank(e.review_priority))

    if limit is not None:
        entries = entries[:limit]

    return tuple(entries)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_entry_from_result(
    result: GuardedApplyExecutionResult,
) -> ApplyReviewQueueEntry:
    """Derive a single queue entry from a ``GuardedApplyExecutionResult``.

    This is a pure data transform. It does not read any database beyond
    what is already encoded in ``result``, and it never writes anything.
    """
    import hashlib

    raw = f"{result.plan_id}|{result.idempotency_key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    execution_id = f"exec-{digest[:16]}"

    executed = result.operated_executed
    blocked = result.operated_blocked
    skipped = result.operated_skipped

    is_blocking = result.execution_status in (
        ApplyExecutionStatus.BLOCKED,
        ApplyExecutionStatus.CONFLICT,
    )
    is_partially_blocked = result.execution_status == ApplyExecutionStatus.PARTIALLY_BLOCKED

    requires_review = _requires_human_review(result)
    priority = _derive_priority(result)

    # Collect reason codes from operation-level blocked reasons
    reason_codes: list[str] = []
    if result.block_reason:
        reason_codes.append(result.block_reason)
    for op in result.results:
        for br in op.guard_blocked_reasons:
            if br and br not in reason_codes:
                reason_codes.append(br)

    # Collect operation refs
    operation_refs = tuple(op.operation_id for op in result.results)

    # Collect and sort mutation types
    mutation_types = tuple(sorted({r.mutation_type for r in result.results if r.mutation_type}))

    # Build human summary
    human_summary = _build_human_summary(
        result.execution_status, executed, blocked, skipped, result.is_dry_run
    )

    return ApplyReviewQueueEntry(
        execution_id=execution_id,
        idempotency_key=result.idempotency_key,
        plan_id=result.plan_id,
        execution_status=result.execution_status,
        review_priority=priority,
        requires_human_review=requires_review,
        is_blocking=is_blocking,
        is_partially_blocked=is_partially_blocked,
        operations_executed=executed,
        operations_blocked=blocked,
        operations_skipped=skipped,
        total_operations=result.total_operations,
        block_reason=result.block_reason,
        reason_codes=tuple(reason_codes),
        human_summary=human_summary,
        executed_at=result.executed_at,
        is_dry_run=result.is_dry_run,
        guard_decision_refs=result.guard_decision_refs,
        mutation_types=mutation_types,
        operation_refs=operation_refs,
    )


def _review_priority_rank(priority: ReviewPriority) -> int:
    """Sort HIGH before MEDIUM before LOW."""
    if priority == ReviewPriority.HIGH:
        return 0
    if priority == ReviewPriority.MEDIUM:
        return 1
    return 2


def _requires_human_review(result: GuardedApplyExecutionResult) -> bool:
    """Return True when the execution result should be flagged for human
    review.

    Rules:
    - BLOCKED, PARTIALLY_BLOCKED, or CONFLICT status always requires review.
    - UNSUPPORTED operations that would otherwise go unnoticed require review.
    """
    if result.execution_status in (
        ApplyExecutionStatus.BLOCKED,
        ApplyExecutionStatus.PARTIALLY_BLOCKED,
        ApplyExecutionStatus.CONFLICT,
        ApplyExecutionStatus.UNSUPPORTED,
    ):
        return True
    for r in result.results:
        if r.execution_status in (
            ApplyExecutionStatus.UNSUPPORTED,
            ApplyExecutionStatus.BLOCKED,
            ApplyExecutionStatus.CONFLICT,
            ApplyExecutionStatus.PARTIALLY_BLOCKED,
        ):
            return True
    return False


def _derive_priority(result: GuardedApplyExecutionResult) -> ReviewPriority:
    """Derive review priority from the execution result.

    Rules:
    - HIGH: execution_status is BLOCKED, PARTIALLY_BLOCKED, or CONFLICT,
      or any operation has blocked/conflict status.
    - MEDIUM: unsupported operations exist, or dry-run with non-empty
      mutation payloads.
    - LOW: all operations executed, no blocked/conflict/unsupported
      statuses.
    """
    if result.execution_status in (
        ApplyExecutionStatus.BLOCKED,
        ApplyExecutionStatus.PARTIALLY_BLOCKED,
        ApplyExecutionStatus.CONFLICT,
    ):
        return ReviewPriority.HIGH

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

    if result.is_dry_run and result.results:
        return ReviewPriority.MEDIUM

    return ReviewPriority.LOW


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
    "ApplyReviewQueueEntry",
    "build_apply_execution_review_queue",
]
