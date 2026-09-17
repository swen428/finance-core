"""Final Transaction Reconciliation Adapter v1 -- narrow, explicit adapter
boundary between reconciliation review/apply outputs and the existing
final-mutation workflow.

This adapter is intentionally **read-only**. It converts eligible
reconciliation review/apply context into deterministic adapter output,
but never:

* executes the final mutation workflow automatically
* creates or updates final transaction records
* opens live database connections
* treats ``ApplyOrchestrationStatus.COMPLETED`` as finalization
* treats dry-run guarded execution as final write authorization

Key invariants:

* No final financial record mutation.
* No settlement obligation generation.
* No Telegram / OCR / PDF / Metabase interaction.
* No write to ``database/finance.db``.
* Deterministic: same input always produces the same adapter result.
* Final-write boundary preserved: ``requires_final_write_confirmation`` is
  always ``True`` for any path that could lead to final mutation.
* No persistence introduced by this adapter unless existing repositories
  already provide the necessary boundary and the need is proven.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from finance_core.reconciliation.apply_orchestrator import (
    ApplyOrchestrationResult,
    ApplyOrchestrationStatus,
)
from finance_core.reconciliation.models import (
    ApplyExecutionStatus,
)

# ---------------------------------------------------------------------------
# Adapter status enum
# ---------------------------------------------------------------------------


class FinalTransactionAdapterStatus(str, Enum):
    """Stable status codes for the final transaction reconciliation adapter.

    ``ELIGIBLE`` means the guarded dry-run apply result is eligible for a
    later human-confirmed final mutation proposal handling. It does **not**
    mean the final mutation has been executed or authorized.

    All other statuses mean the apply result is ineligible for final
    mutation and must be routed to human review.
    """

    ELIGIBLE = "eligible"
    """All guard-cleared operations executed (dry-run). Eligible for later
    human-confirmed final mutation proposal. Not finalized."""

    INELIGIBLE_BLOCKED = "ineligible_blocked"
    """The guard blocked all operations. No execution path succeeded."""

    INELIGIBLE_PARTIALLY_BLOCKED = "ineligible_partially_blocked"
    """Some operations executed and some were blocked by the guard."""

    INELIGIBLE_CONFLICT = "ineligible_conflict"
    """An idempotency-key conflict was detected by the guarded runtime."""

    INELIGIBLE_UNSUPPORTED = "ineligible_unsupported"
    """The plan had no operations to execute."""

    INELIGIBLE_REQUIRES_REVIEW = "ineligible_requires_review"
    """Execution finished but the review summary flags human review."""


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------


class FinalTransactionAdapterReason(str, Enum):
    """Stable reason codes explaining why an adapter result is ineligible.

    These codes are deterministic and audit-friendly. They never change
    between adapter versions for the same input condition.
    """

    ORCHESTRATION_BLOCKED = "orchestration_blocked"
    ORCHESTRATION_PARTIALLY_BLOCKED = "orchestration_partially_blocked"
    ORCHESTRATION_CONFLICT = "orchestration_conflict"
    ORCHESTRATION_UNSUPPORTED = "orchestration_unsupported"
    ORCHESTRATION_REQUIRES_REVIEW = "orchestration_requires_review"
    MISSING_OPERATION_RESULTS = "missing_operation_results"
    OPERATION_NOT_EXECUTED = "operation_not_executed"
    OPERATION_NOT_GUARD_APPROVED = "operation_not_guard_approved"
    MISSING_GUARD_DECISION_REFS = "missing_guard_decision_refs"
    INVALID_INPUT = "invalid_input"


# ---------------------------------------------------------------------------
# Operation summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationSummary:
    """Per-operation status summary preserved from the guarded apply
    execution result.

    Operation-level statuses from the underlying
    ``GuardedApplyExecutionResult`` are never lost. This summary captures
    the key fields needed for adapter eligibility decisions and human
    review traceability.
    """

    operation_id: str
    execution_status: ApplyExecutionStatus
    guard_decision_approved: bool
    mutation_type: str
    reason: str
    guard_decision_idempotency_key: str | None = None


# ---------------------------------------------------------------------------
# Adapter input DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalTransactionAdapterInput:
    """Typed input for the final transaction reconciliation adapter.

    Carries the full ``ApplyOrchestrationResult`` produced by
    ``orchestrate_reconciliation_apply()``. The adapter reads the
    orchestration status, per-operation execution results, guard
    decision references, and review summary to determine eligibility
    for a future human-confirmed final mutation proposal.

    The adapter never mutates the input or any external state.
    """

    orchestration_result: ApplyOrchestrationResult


# ---------------------------------------------------------------------------
# Adapter result DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalTransactionAdapterResult:
    """Immutable result of the final transaction reconciliation adapter.

    Carries eligibility status, per-operation summaries, guard decision
    references, reason codes, and an explicit
    ``requires_final_write_confirmation`` flag. Every field is derived
    deterministically from the input; none are random, time-dependent,
    or AI-generated.

    The ``requires_final_write_confirmation`` flag is always ``True``
    for the ``ELIGIBLE`` path, enforcing the invariant that a separate
    human-confirmed final-write step is still required before any final
    financial transaction is created or adjusted.
    """

    status: FinalTransactionAdapterStatus
    """Top-level adapter status derived from orchestration status."""

    is_eligible_for_final_mutation: bool
    """``True`` only when status is ``ELIGIBLE``. Even when ``True``,
    a separate human-confirmed final-write step is required."""

    requires_final_write_confirmation: bool
    """Always ``True`` for any path that could lead to final mutation.
    For ineligible paths this is ``False`` — there is nothing to confirm."""

    plan_id: str
    """The plan id from the orchestration result (echoed for traceability)."""

    idempotency_key: str
    """The idempotency key from the orchestration result (echoed)."""

    orchestration_status: ApplyOrchestrationStatus
    """The original orchestration status from the input (echoed)."""

    operation_summaries: tuple[OperationSummary, ...]
    """Per-operation status summaries preserved from the guarded apply
    execution result. Never empty when input is valid."""

    total_operations: int
    """Total number of operations in the execution result."""

    eligible_operation_count: int
    """Number of operations that are guard-cleared and ``EXECUTED``."""

    ineligible_operation_count: int
    """Number of operations that are not eligible for final mutation."""

    guard_decision_refs: tuple[str, ...]
    """Guard decision references from the execution result."""

    reason_codes: tuple[str, ...]
    """Stable reason codes (from ``FinalTransactionAdapterReason``) explaining
    why the result is ineligible. Empty tuple when status is ``ELIGIBLE``."""

    adapter_note: str
    """Human-readable, deterministic adapter explanation string."""


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_final_transaction_reconciliation_adapter_result(
    adapter_input: FinalTransactionAdapterInput,
) -> FinalTransactionAdapterResult:
    """Build a deterministic adapter result from an orchestration result.

    The adapter inspects the orchestration status and per-operation
    execution results to determine whether the guarded dry-run apply
    output is eligible for a later human-confirmed final mutation
    proposal.

    **Eligibility rules (deterministic):**

    * ``ApplyOrchestrationStatus.COMPLETED`` with all operations
      guard-cleared and ``EXECUTED`` → ``ELIGIBLE`` (but still requires
      human final-write confirmation via
      ``requires_final_write_confirmation=True``).

    * ``BLOCKED`` → ``INELIGIBLE_BLOCKED``

    * ``PARTIALLY_BLOCKED`` → ``INELIGIBLE_PARTIALLY_BLOCKED``

    * ``CONFLICT`` → ``INELIGIBLE_CONFLICT``

    * ``UNSUPPORTED`` → ``INELIGIBLE_UNSUPPORTED``

    * ``REQUIRES_REVIEW`` → ``INELIGIBLE_REQUIRES_REVIEW``

    * Missing guard decisions or missing operation references are not
      silently accepted; they produce an ineligible result with
      explicit reason codes.

    * Operation-level statuses are preserved via
      ``OperationSummary`` objects on the result.

    **What the adapter never does:**

    * Never calls ``execute_guarded_final_mutation_workflow``.
    * Never creates or updates final transactions.
    * Never opens a database connection.
    * Never assumes that ``COMPLETED`` means finalization.
    * Never treats dry-run guarded execution as final write authorization.

    Args:
        adapter_input: Typed adapter input wrapping an
            ``ApplyOrchestrationResult``.

    Returns:
        A ``FinalTransactionAdapterResult`` with eligibility status,
        operation summaries, and reason codes.

    Raises:
        TypeError: When ``adapter_input`` is not a
            ``FinalTransactionAdapterInput``.
        ValueError: When the orchestration result on the input is ``None``.
    """
    if not isinstance(adapter_input, FinalTransactionAdapterInput):
        raise TypeError(
            "adapter_input must be a FinalTransactionAdapterInput, "
            f"got {type(adapter_input).__name__}"
        )

    orch_result = adapter_input.orchestration_result

    if orch_result is None:
        raise ValueError("orchestration_result must not be None")

    orch_status = orch_result.status

    # -- Build operation summaries -------------------------------------------
    op_summaries = _build_operation_summaries(orch_result)

    total_ops = len(op_summaries)
    eligible_count = sum(
        1
        for s in op_summaries
        if s.execution_status == ApplyExecutionStatus.EXECUTED and s.guard_decision_approved
    )
    ineligible_count = total_ops - eligible_count

    # -- Determine eligibility ------------------------------------------------
    status, reason_codes = _derive_adapter_status_and_reasons(
        orch_status, op_summaries, orch_result
    )

    is_eligible = status == FinalTransactionAdapterStatus.ELIGIBLE
    # Requires confirmation for any eligible path; False for ineligible paths
    # (there is nothing to confirm when the adapter says it's ineligible).
    requires_confirmation = is_eligible

    # -- Build adapter note ---------------------------------------------------
    note = _build_adapter_note(status, eligible_count, ineligible_count, total_ops)

    return FinalTransactionAdapterResult(
        status=status,
        is_eligible_for_final_mutation=is_eligible,
        requires_final_write_confirmation=requires_confirmation,
        plan_id=orch_result.plan.plan_id,
        idempotency_key=orch_result.idempotency_key,
        orchestration_status=orch_status,
        operation_summaries=op_summaries,
        total_operations=total_ops,
        eligible_operation_count=eligible_count,
        ineligible_operation_count=ineligible_count,
        guard_decision_refs=orch_result.execution_result.guard_decision_refs,
        reason_codes=tuple(r.value for r in reason_codes),
        adapter_note=note,
    )


# ---------------------------------------------------------------------------
# Internal helpers -- deterministic, no side effects
# ---------------------------------------------------------------------------


def _build_operation_summaries(
    orch_result: ApplyOrchestrationResult,
) -> tuple[OperationSummary, ...]:
    """Build per-operation summaries from the execution result.

    Preserves operation-level statuses so no per-operation detail is
    lost in the adapter result.
    """
    exec_result = orch_result.execution_result

    if not exec_result.results:
        return ()

    summaries: list[OperationSummary] = []
    for op in exec_result.results:
        summaries.append(
            OperationSummary(
                operation_id=op.operation_id,
                execution_status=op.execution_status,
                guard_decision_approved=op.guard_decision_approved,
                mutation_type=op.mutation_type,
                reason=op.reason,
                guard_decision_idempotency_key=op.guard_decision_idempotency_key,
            )
        )
    return tuple(summaries)


def _derive_adapter_status_and_reasons(
    orch_status: ApplyOrchestrationStatus,
    op_summaries: tuple[OperationSummary, ...],
    orch_result: ApplyOrchestrationResult,
) -> tuple[FinalTransactionAdapterStatus, tuple[FinalTransactionAdapterReason, ...]]:
    """Derive the adapter status and reason codes from the orchestration
    status and operation summaries.

    The mapping from ``ApplyOrchestrationStatus`` to
    ``FinalTransactionAdapterStatus`` is fixed and deterministic.
    """
    # -- Direct status-based mapping for explicitly blocked / conflict /
    #    unsupported statuses (these take priority over operation-level
    #    checks because the plan-level status is the authoritative signal). --
    if orch_status == ApplyOrchestrationStatus.UNSUPPORTED:
        return (
            FinalTransactionAdapterStatus.INELIGIBLE_UNSUPPORTED,
            (FinalTransactionAdapterReason.ORCHESTRATION_UNSUPPORTED,),
        )

    if orch_status == ApplyOrchestrationStatus.BLOCKED:
        return (
            FinalTransactionAdapterStatus.INELIGIBLE_BLOCKED,
            (FinalTransactionAdapterReason.ORCHESTRATION_BLOCKED,),
        )

    if orch_status == ApplyOrchestrationStatus.PARTIALLY_BLOCKED:
        return (
            FinalTransactionAdapterStatus.INELIGIBLE_PARTIALLY_BLOCKED,
            (FinalTransactionAdapterReason.ORCHESTRATION_PARTIALLY_BLOCKED,),
        )

    if orch_status == ApplyOrchestrationStatus.CONFLICT:
        return (
            FinalTransactionAdapterStatus.INELIGIBLE_CONFLICT,
            (FinalTransactionAdapterReason.ORCHESTRATION_CONFLICT,),
        )

    if orch_status == ApplyOrchestrationStatus.REQUIRES_REVIEW:
        return (
            FinalTransactionAdapterStatus.INELIGIBLE_REQUIRES_REVIEW,
            (FinalTransactionAdapterReason.ORCHESTRATION_REQUIRES_REVIEW,),
        )

    # -- Missing operations check (only applies after direct status checks) ---
    if not op_summaries:
        return (
            FinalTransactionAdapterStatus.INELIGIBLE_UNSUPPORTED,
            (FinalTransactionAdapterReason.MISSING_OPERATION_RESULTS,),
        )

    # -- COMPLETED: verify per-operation eligibility -----------------------
    if orch_status == ApplyOrchestrationStatus.COMPLETED:
        # Even when COMPLETED, verify that every operation is EXECUTED
        # and guard-approved. If any operation is not, the result is not
        # truly eligible.
        non_executed = [
            s for s in op_summaries if s.execution_status != ApplyExecutionStatus.EXECUTED
        ]
        non_guard_approved = [s for s in op_summaries if not s.guard_decision_approved]

        reasons: list[FinalTransactionAdapterReason] = []

        if non_executed:
            reasons.append(FinalTransactionAdapterReason.OPERATION_NOT_EXECUTED)

        if non_guard_approved:
            reasons.append(FinalTransactionAdapterReason.OPERATION_NOT_GUARD_APPROVED)

        if not orch_result.execution_result.guard_decision_refs:
            reasons.append(FinalTransactionAdapterReason.MISSING_GUARD_DECISION_REFS)

        if reasons:
            return (
                FinalTransactionAdapterStatus.INELIGIBLE_REQUIRES_REVIEW,
                tuple(reasons),
            )

        return (
            FinalTransactionAdapterStatus.ELIGIBLE,
            (),
        )

    # Defensive: any future orchestration status not explicitly mapped above
    # surfaces as REQUIRES_REVIEW rather than ELIGIBLE.
    return (
        FinalTransactionAdapterStatus.INELIGIBLE_REQUIRES_REVIEW,
        (FinalTransactionAdapterReason.ORCHESTRATION_REQUIRES_REVIEW,),
    )


def _build_adapter_note(
    status: FinalTransactionAdapterStatus,
    eligible_count: int,
    ineligible_count: int,
    total_ops: int,
) -> str:
    """Build a concise, deterministic human-readable adapter note."""
    if status == FinalTransactionAdapterStatus.ELIGIBLE:
        return (
            f"Final transaction adapter {status.value}: "
            f"{eligible_count} of {total_ops} operation(s) eligible. "
            f"Requires separate human-confirmed final-write step before any "
            f"final financial record is created or modified. "
            f"Guarded dry-run only -- no final mutation executed."
        )

    return (
        f"Final transaction adapter {status.value}: "
        f"{eligible_count} eligible, {ineligible_count} ineligible "
        f"of {total_ops} operation(s). "
        f"Ineligible -- routed to human review. "
        f"No final financial records were created or modified."
    )


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------

__all__ = [
    "FinalTransactionAdapterStatus",
    "FinalTransactionAdapterReason",
    "FinalTransactionAdapterInput",
    "FinalTransactionAdapterResult",
    "OperationSummary",
    "build_final_transaction_reconciliation_adapter_result",
]
