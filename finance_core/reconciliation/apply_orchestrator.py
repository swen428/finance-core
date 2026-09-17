"""Reconciliation Apply Orchestrator v1 -- a single high-level, deterministic
entry point that coordinates the existing reconciliation apply components into
one bounded orchestration call.

The orchestrator is intentionally **thin**. It wires together already-defined,
independently-tested components in a fixed order and surfaces a structured
result. It does **not** re-implement planning, guarding, guarded execution,
persistence, or review-summary logic, and it never becomes the monetary
authority.

Coordinated pipeline (every step already existed before this module)::

    ApplyPlanInput[*]
           │  build_reconciliation_apply_plan()
           ▼
    ReconciliationApplyPlan
           │  GuardedApplyRuntime.execute(... guard_decisions_by_operation_id ...)
           ▼
    GuardedApplyExecutionResult   (dry-run only -- never mutates final txns)
           │  build_guarded_apply_execution_review_summary()
           ▼
    GuardedApplyExecutionReviewSummary
           │  (optional) GuardedApplyExecutionRepository durable idempotency + persistence

Key invariants:
- No final financial record mutation. The orchestrator never calls the final
  mutation workflow (``execute_guarded_final_mutation_workflow``); that is a
  separate, human-confirmed boundary. ``GuardedApplyRuntime`` is a dry-run
  runtime, so "executed" here means guard-cleared and recorded, not finalized.
- "confirmed" / "planned" / "executed" are never treated as finalized. A
  guarded ``EXECUTED`` status is still a dry-run outcome.
- Blocked / partially-blocked / conflict / unsupported outcomes are preserved
  and surfaced as review-required; they are never converted into success.
- Operation-level statuses are not lost: the full ``GuardedApplyExecutionResult``
  (with per-operation ``GuardedOperationResult`` rows) is carried on the result.
- Persistence is optional and explicit: a ``repository`` is only used when the
  caller supplies one. Durable idempotency is delegated to
  ``GuardedApplyRuntime(repository=...)`` and the repository; this module
  invents no new persistence semantics and bypasses no idempotency key.
- Deterministic: same inputs + same idempotency key + same guard decisions
  always produce the same orchestration result (modulo the runtime clock).
- No write to ``database/finance.db``. The orchestrator never opens a database;
  any connection is owned and supplied by the caller via the repository.
- No Telegram / OCR / PDF / Metabase interaction. No settlement generation.
- AI is never the final monetary authority: guard decisions are deterministic
  outputs of ``FinalMutationGuard`` and human reviewer decisions; the
  orchestrator only routes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

from finance_core.reconciliation.apply_execution_review import (
    GuardedApplyExecutionReviewSummary,
    build_guarded_apply_execution_review_summary,
)
from finance_core.reconciliation.apply_plan import (
    ApplyPlanInput,
    ReconciliationApplyPlan,
    build_reconciliation_apply_plan,
)
from finance_core.reconciliation.apply_runtime import (
    GuardedApplyRuntime,
    build_guarded_apply_execution_fingerprint,
)
from finance_core.reconciliation.final_mutation_proposal import FinalMutationGuardDecision
from finance_core.reconciliation.models import (
    ApplyExecutionStatus,
    GuardedApplyExecutionResult,
)

if TYPE_CHECKING:
    from finance_core.reconciliation.apply_runtime_persistence import (
        GuardedApplyExecutionRepository,
    )

# ---------------------------------------------------------------------------
# Orchestration status
# ---------------------------------------------------------------------------


class ApplyOrchestrationStatus(str, Enum):
    """Stable top-level status for an apply orchestration attempt.

    Derived from the underlying ``ApplyExecutionStatus`` produced by the
    guarded apply runtime. ``COMPLETED`` corresponds to a fully guard-cleared
    dry-run execution; it is **not** a finalized mutation. Any blocked,
    partially-blocked, conflict, or unsupported outcome is surfaced as a
    non-completed status and requires human review.
    """

    COMPLETED = "completed"
    """All operations were guard-cleared and recorded (dry-run). Not finalized."""

    BLOCKED = "blocked"
    """The guard blocked all operations; no execution path succeeded."""

    PARTIALLY_BLOCKED = "partially_blocked"
    """Some operations executed and some were blocked by the guard."""

    REQUIRES_REVIEW = "requires_review"
    """Execution finished but the review summary flags human review (e.g. unsupported)."""

    CONFLICT = "conflict"
    """An idempotency-key conflict was detected by the guarded runtime."""

    UNSUPPORTED = "unsupported"
    """The plan had no operations to execute (unsupported plan shape)."""


# ---------------------------------------------------------------------------
# Apply idempotency classification (v1)
# ---------------------------------------------------------------------------


class ApplyIdempotencyOutcome(str, Enum):
    """Stable classification of a repeat apply attempt against a persisted
    execution fingerprint.

    This is a **read-only** classification of what would happen if the caller
    re-applied the same idempotency key with the supplied plan content. It
    never executes the guarded runtime, never persists anything, and never
    mutates final financial records. It only inspects persisted state.

    The five prior-execution outcomes required by the apply idempotency v1
    contract map onto this enum:

    - a prior ``EXECUTED`` execution with a matching fingerprint -> ``IDEMPOTENT_REPLAY``
    - a prior execution with the same key but a materially different
      fingerprint -> ``CONFLICT`` (duplicate-prevention, safe rejection)
    - a prior ``BLOCKED`` execution -> ``PRIOR_BLOCKED`` (not eligible without
      new valid inputs)
    - a prior ``PARTIALLY_BLOCKED`` execution -> ``PRIOR_PARTIALLY_BLOCKED``
      (remains review-required on retry)
    - a prior ``CONFLICT`` or ``UNSUPPORTED`` execution -> ``PRIOR_REQUIRES_REVIEW``
      (remains review-required / safely blocked on retry)
    - no prior execution for the key -> ``NO_PRIOR_EXECUTION``
    """

    IDEMPOTENT_REPLAY = "idempotent_replay"
    """A prior execution with the same key and same fingerprint exists. A
    repeat apply would be recognized as the same completed/recorded
    execution and would not insert a duplicate execution or operation row."""

    CONFLICT = "conflict"
    """The idempotency key already exists with a materially different
    fingerprint. A repeat apply would be safely rejected as a conflicting
    execution (never silently accepted)."""

    PRIOR_BLOCKED = "prior_blocked"
    """A prior execution exists but its status is ``BLOCKED``. A retry would
    re-run the guard and stay blocked unless new, valid inputs change the
    fingerprint; the prior blocked execution does not become eligible on
    retry without new valid inputs."""

    PRIOR_PARTIALLY_BLOCKED = "prior_partially_blocked"
    """A prior execution exists with ``PARTIALLY_BLOCKED`` status. A retry
    remains review-required: the partially blocked outcome is not converted
    into a completed result."""

    PRIOR_REQUIRES_REVIEW = "prior_requires_review"
    """A prior execution exists with ``CONFLICT`` or ``UNSUPPORTED`` status.
    A retry remains review-required / safely blocked; the prior outcome is
    not converted into a completed result."""

    NO_PRIOR_EXECUTION = "no_prior_execution"
    """No prior execution exists for the idempotency key. A first apply
    would record the execution fingerprint normally."""


@dataclass(frozen=True)
class ApplyIdempotencyClassification:
    """Immutable read-only classification of a repeat apply attempt.

    Carries the ``outcome``, the prior execution audit reference (when one
    exists), and the candidate fingerprint for the supplied plan content so
    callers can compare it to the persisted fingerprint without re-running
    the guarded runtime.

    The ``prior_*`` audit fields preserve audit evidence from the original
    execution so callers can explain what happened and why without executing
    again. They are ``None`` (or empty) when no prior execution exists.
    """

    outcome: ApplyIdempotencyOutcome
    """The stable classification of the repeat attempt."""

    idempotency_key: str
    """The idempotency key being classified (echoed for traceability)."""

    candidate_fingerprint: str
    """The deterministic execution fingerprint of the supplied plan content.
    Identical to what ``build_guarded_apply_execution_fingerprint`` and the
    guarded runtime compute, so callers can compare it to
    ``prior_execution_fingerprint`` directly."""

    prior_execution_exists: bool
    """True when any execution exists for the idempotency key in the
    repository."""

    prior_execution_id: str | None = None
    """The execution id of the prior execution, when one exists. ``None``
    when no prior execution exists."""

    prior_execution_status: ApplyExecutionStatus | None = None
    """The execution status of the prior execution, when one exists. ``None``
    when no prior execution exists."""

    prior_execution_fingerprint: str | None = None
    """The persisted execution fingerprint of the prior execution, when one
    exists. ``None`` when no prior execution exists."""

    prior_block_reason: str = ""
    """The plan-level block reason of the prior execution, when one exists.
    Empty string when no prior execution exists."""

    prior_executed_at: str = ""
    """The ``executed_at`` timestamp of the prior execution, when one exists.
    Empty string when no prior execution exists."""

    classification_note: str = ""
    """Human-readable, deterministic classification explanation."""

    @property
    def fingerprint_matches_prior(self) -> bool:
        """True when a prior execution exists and its persisted fingerprint
        matches the candidate fingerprint for the supplied plan content."""
        if not self.prior_execution_exists or self.prior_execution_fingerprint is None:
            return False
        return self.prior_execution_fingerprint == self.candidate_fingerprint

    @property
    def is_same_recorded_execution(self) -> bool:
        """True when the repeat attempt matches the same recorded execution.

        This means a prior execution exists and its persisted fingerprint
        equals the candidate fingerprint, so a repeat apply with this key and
        plan content would be recognized as the same recorded execution (the
        guarded runtime returns the cached prior result and the repository
        does not insert a new row). True for ``IDEMPOTENT_REPLAY``,
        ``PRIOR_BLOCKED``, ``PRIOR_PARTIALLY_BLOCKED``, and
        ``PRIOR_REQUIRES_REVIEW``. False for ``NO_PRIOR_EXECUTION`` (no prior
        to match) and ``CONFLICT`` (materially different payload, safely
        rejected). The non-completed same-fingerprint outcomes are included
        because the prior recorded state is preserved, not overwritten.
        """
        return self.fingerprint_matches_prior and self.outcome in (
            ApplyIdempotencyOutcome.IDEMPOTENT_REPLAY,
            ApplyIdempotencyOutcome.PRIOR_BLOCKED,
            ApplyIdempotencyOutcome.PRIOR_PARTIALLY_BLOCKED,
            ApplyIdempotencyOutcome.PRIOR_REQUIRES_REVIEW,
        )

    @property
    def is_duplicate_prevention_rejection(self) -> bool:
        """True when the repeat attempt was safely rejected as a duplicate.

        This means the idempotency key already exists with a materially
        different fingerprint, so a repeat apply would be rejected as a
        conflict rather than silently accepted. True only for ``CONFLICT``.
        No prior recorded execution is inserted by the rejected attempt.
        """
        return self.outcome is ApplyIdempotencyOutcome.CONFLICT

    @property
    def is_completed_idempotent_replay(self) -> bool:
        """True only when the repeat attempt is an idempotent replay of a
        completed (prior ``EXECUTED``) execution.

        Narrower than ``is_same_recorded_execution``: a same-fingerprint prior
        that was ``BLOCKED`` / ``PARTIALLY_BLOCKED`` / ``UNSUPPORTED`` /
        ``CONFLICT`` is the same recorded execution, but it is not a replay of
        a *completed* execution. Use this when a caller specifically needs to
        distinguish "recognized as the same completed run" from "recognized as
        the same non-completed recorded run."
        """
        return self.outcome is ApplyIdempotencyOutcome.IDEMPOTENT_REPLAY


# ---------------------------------------------------------------------------
# Orchestration input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyOrchestrationInput:
    """Typed input for ``orchestrate_reconciliation_apply``.

    Carries the structured plan inputs, the per-operation guard decisions, and
    the idempotency key. Every operation in the resulting plan must have a
    matching entry in ``guard_decisions_by_operation_id`` keyed by
    ``ApplyPlanOperation.operation_id``; otherwise the guarded runtime blocks
    the plan with a missing-guard decision (this is the runtime's existing
    behaviour, preserved by the orchestrator).

    Attributes
    ----------
    plan_inputs:
        One ``ApplyPlanInput`` per review queue item being applied.
    guard_decisions_by_operation_id:
        Guard decisions keyed by the operation id that the plan builder will
        emit (``op-<decision_id>``). These are deterministic outputs of
        ``FinalMutationGuard``/human review, never AI free-text authority.
    idempotency_key:
        Stable key for guarded execution idempotency. Replaying the same key
        with the same content is safe; replaying with different content is a
        conflict, surfaced by the runtime and preserved here.
    plan_label:
        Optional label passed to the plan builder for plan-id derivation.
    """

    plan_inputs: tuple[ApplyPlanInput, ...]
    guard_decisions_by_operation_id: dict[str, FinalMutationGuardDecision]
    idempotency_key: str
    plan_label: str = ""


# ---------------------------------------------------------------------------
# Orchestration result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyOrchestrationResult:
    """Immutable result of ``orchestrate_reconciliation_apply``.

    Exposes enough information for a caller to understand whether execution
    completed, was blocked, was partially blocked, requires human review, or
    hit an idempotency conflict -- and whether persistence occurred -- without
    ever conflating a guard-cleared dry-run with a finalized mutation.

    The full underlying execution result and review summary are carried by
    reference so operation-level statuses are not lost.
    """

    status: ApplyOrchestrationStatus
    """Top-level orchestration status derived from the guarded execution result."""

    plan: ReconciliationApplyPlan
    """The dry-run apply plan that was coordinated (``is_dry_run=True``)."""

    execution_result: GuardedApplyExecutionResult
    """Full guarded execution result, including per-operation statuses."""

    review_summary: GuardedApplyExecutionReviewSummary
    """Deterministic read-only review summary derived from the execution result."""

    persisted: bool
    """True when the execution result was durably persisted via the repository.

    Always ``False`` when no repository was supplied (dry-run / no-persistence
    path). When a repository is supplied, this reflects whether a new row was
    inserted by the repository's idempotent save (``False`` on idempotent
    replay of identical content, ``True`` on first write).
    """

    persistence_enabled: bool
    """True when a repository was supplied to the orchestrator call.

    Makes the persistence boundary explicit and testable independently of
    whether this particular call inserted a new row.
    """

    idempotency_key: str
    """The idempotency key used for guarded execution (echoed for traceability)."""

    orchestration_note: str
    """Human-readable, deterministic orchestration summary string."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def orchestrate_reconciliation_apply(
    orch_input: ApplyOrchestrationInput,
    *,
    repository: GuardedApplyExecutionRepository | None = None,
    runtime: GuardedApplyRuntime | None = None,
    clock: Callable[[], str] | None = None,
) -> ApplyOrchestrationResult:
    """Coordinate the reconciliation apply pipeline in one deterministic call.

    The orchestrator performs, in order:

    1. Build a dry-run ``ReconciliationApplyPlan`` from ``orch_input.plan_inputs``
       via the existing ``build_reconciliation_apply_plan`` builder.
    2. Execute the plan through ``GuardedApplyRuntime``, passing the caller's
       per-operation guard decisions and idempotency key. When a ``repository``
       is supplied, the runtime is constructed with it so durable idempotency
       and persistence are delegated to the existing runtime/repository path.
    3. Build a deterministic read-only
       ``GuardedApplyExecutionReviewSummary`` from the execution result.
    4. Record whether persistence occurred (delegated to the runtime/repository;
       see ``ApplyOrchestrationResult.persisted``).

    The orchestrator does **not** call the final mutation workflow, does **not**
    open any database, and does **not** mutate final financial records. A
    ``COMPLETED`` status means the guarded dry-run runtime guard-cleared and
    recorded every operation; it is not a finalized mutation.

    Args:
        orch_input: Typed orchestration input (plan inputs, guard decisions,
            idempotency key).
        repository: Optional durable persistence adapter. When supplied, the
            guarded runtime uses it for durable idempotency and persists the
            execution result. When omitted, behaviour is in-memory only.
        runtime: Optional pre-constructed ``GuardedApplyRuntime``. When
            supplied, the orchestrator uses it as-is (including any repository
            it already holds) and ignores the ``repository`` argument. When
            omitted, a fresh runtime is constructed with ``repository`` (which
            may be ``None``).
        clock: Optional deterministic clock for the guarded runtime's
            ``executed_at`` timestamp. Useful for tests.

    Returns:
        An ``ApplyOrchestrationResult`` carrying the plan, the full guarded
        execution result, the review summary, and persistence flags.
    """
    if not orch_input.idempotency_key:
        raise ValueError("orchestrate_reconciliation_apply requires a non-empty idempotency_key")
    if not orch_input.plan_inputs:
        raise ValueError("orchestrate_reconciliation_apply requires at least one plan input")

    # 1. Build the dry-run plan via the existing, tested builder.
    plan = build_reconciliation_apply_plan(
        list(orch_input.plan_inputs),
        plan_label=orch_input.plan_label,
    )

    # 2. Execute through the guarded runtime. The runtime is the control point
    #    for idempotency, guard evaluation, and (when configured) durable
    #    persistence. The orchestrator never bypasses it.
    #
    #    When the orchestrator owns the repository (no caller-supplied runtime),
    #    capture whether the idempotency key already existed *before* execution
    #    so ``persisted`` can distinguish a first write from an idempotent
    #    replay. The caller-supplied-runtime path cannot peek at the runtime's
    #    private repository, so it reports persistence at the boundary level
    #    (see ``_did_persist_new_row``).
    owned_repository = repository if runtime is None else None
    existed_before: bool | None = None
    if owned_repository is not None:
        existed_before = owned_repository.has_idempotency_key(orch_input.idempotency_key)

    active_runtime = runtime if runtime is not None else GuardedApplyRuntime(repository=repository)
    execution_result = active_runtime.execute(
        plan,
        guard_decisions_by_operation_id=orch_input.guard_decisions_by_operation_id,
        idempotency_key=orch_input.idempotency_key,
        clock=clock,
    )

    # 3. Build the deterministic read-only review summary.
    review_summary = build_guarded_apply_execution_review_summary(execution_result)

    # 4. Determine persistence flags. The runtime persists via the repository
    #    it was constructed with; the orchestrator only reports the boundary.
    persistence_enabled = active_runtime.has_persistence
    persisted = _did_persist_new_row(
        execution_result,
        persistence_enabled=persistence_enabled,
        existed_before=existed_before,
    )

    status = _derive_orchestration_status(execution_result, review_summary)
    note = _build_orchestration_note(status, execution_result, persistence_enabled, persisted)

    return ApplyOrchestrationResult(
        status=status,
        plan=plan,
        execution_result=execution_result,
        review_summary=review_summary,
        persisted=persisted,
        persistence_enabled=persistence_enabled,
        idempotency_key=orch_input.idempotency_key,
        orchestration_note=note,
    )


# ---------------------------------------------------------------------------
# Internal helpers -- deterministic, no side effects
# ---------------------------------------------------------------------------


def _derive_orchestration_status(
    execution_result: GuardedApplyExecutionResult,
    review_summary: GuardedApplyExecutionReviewSummary,
) -> ApplyOrchestrationStatus:
    """Map the guarded execution status to an orchestration status.

    Blocking / conflict / unsupported outcomes are surfaced directly and never
    converted into ``COMPLETED``. ``UNSUPPORTED`` (empty plan) maps to its own
    status so callers can distinguish "nothing to do" from "review needed".
    """
    exec_status = execution_result.execution_status
    if exec_status == ApplyExecutionStatus.EXECUTED:
        return ApplyOrchestrationStatus.COMPLETED
    if exec_status == ApplyExecutionStatus.BLOCKED:
        return ApplyOrchestrationStatus.BLOCKED
    if exec_status == ApplyExecutionStatus.PARTIALLY_BLOCKED:
        return ApplyOrchestrationStatus.PARTIALLY_BLOCKED
    if exec_status == ApplyExecutionStatus.CONFLICT:
        return ApplyOrchestrationStatus.CONFLICT
    if exec_status == ApplyExecutionStatus.UNSUPPORTED:
        return ApplyOrchestrationStatus.UNSUPPORTED
    # Defensive: any future runtime status that requires human review but is
    # not a direct block surfaces as REQUIRES_REVIEW rather than COMPLETED.
    if review_summary.requires_human_review:
        return ApplyOrchestrationStatus.REQUIRES_REVIEW
    return ApplyOrchestrationStatus.COMPLETED


def _did_persist_new_row(
    execution_result: GuardedApplyExecutionResult,
    *,
    persistence_enabled: bool,
    existed_before: bool | None,
) -> bool:
    """Report whether this orchestration call persisted a new execution row.

    Semantics (all delegated to existing components, no new logic):

    - No persistence configured (runtime has no repository): ``False``. This is
      the explicit dry-run / no-persistence path.
    - Idempotent replay (same key + same fingerprint already persisted): the
      runtime returns the cached result and the repository does not insert a
      new row, so ``False``. Detected via ``existed_before=True`` for the
      owned-repository path. Idempotency is preserved, not bypassed.
    - Idempotency conflict (same key + different fingerprint): the runtime
      returns a ``CONFLICT`` result without persisting a new row, so ``False``.
    - First write of a guard-cleared or guard-blocked result: ``True``.

    For caller-supplied runtimes, ``existed_before`` is ``None`` (the
    orchestrator cannot inspect the runtime's private repository). In that case
    a non-conflict result with persistence enabled is reported as persisted:
    the runtime persists every non-conflict result it newly produces. Replay
    detection across calls is the caller's responsibility when it owns the
    runtime, matching the runtime's own documented contract.
    """
    if not persistence_enabled:
        return False

    # CONFLICT results are never persisted as new rows by the runtime.
    if execution_result.execution_status == ApplyExecutionStatus.CONFLICT:
        return False

    if existed_before is not None:
        # Owned-repository path: precise first-write vs. replay detection.
        return not existed_before

    # Caller-supplied-runtime path: report at the boundary level.
    return True


def _build_orchestration_note(
    status: ApplyOrchestrationStatus,
    execution_result: GuardedApplyExecutionResult,
    persistence_enabled: bool,
    persisted: bool,
) -> str:
    """Build a concise, deterministic human-readable orchestration note."""
    if persistence_enabled:
        persistence_clause = "persisted" if persisted else "persistence enabled (no new row)"
    else:
        persistence_clause = "no persistence (dry-run)"

    return (
        f"Apply orchestration {status.value}: "
        f"{execution_result.operated_executed} executed, "
        f"{execution_result.operated_blocked} blocked, "
        f"{execution_result.operated_skipped} skipped. "
        f"{persistence_clause}. "
        f"Guarded dry-run only -- no final financial records were created or modified."
    )


# ---------------------------------------------------------------------------
# Apply idempotency classification (v1) -- read-only, caller-driven
# ---------------------------------------------------------------------------


def classify_reconciliation_apply_idempotency(
    orch_input: ApplyOrchestrationInput,
    *,
    repository: GuardedApplyExecutionRepository,
) -> ApplyIdempotencyClassification:
    """Classify a repeat apply attempt against the persisted execution
    fingerprint, without executing the guarded runtime.

    This is a **read-only** helper. It computes the deterministic execution
    fingerprint of the supplied plan content (the same fingerprint the
    guarded runtime and ``build_guarded_apply_execution_fingerprint`` use),
    loads any prior execution row for the idempotency key from the
    caller-supplied repository, and classifies the repeat attempt into one
    of the six ``ApplyIdempotencyOutcome`` values.

    It never:

    - calls the guarded runtime or final mutation workflow,
    - persists, updates, or deletes anything,
    - opens a database (the connection is owned and supplied by the caller
      via the repository),
    - mutates final financial records,
    - treats dry-run or apply orchestration completion as final financial
      write authorization.

    Classification rules (deterministic, exact order):

    1. No prior execution row for the key -> ``NO_PRIOR_EXECUTION``. A first
       apply would record the execution fingerprint normally.
    2. Prior execution with the **same** fingerprint -> the repeat would be
       recognized as the same recorded execution (the guarded runtime returns
       the cached prior result and the repository does not insert a new row).
       The outcome reflects the prior execution's status so each prior
       execution class has an explicit repeat behaviour:
       - prior ``EXECUTED`` -> ``IDEMPOTENT_REPLAY`` (completed, recognized
         as the same recorded execution, no duplicate operation records)
       - prior ``BLOCKED`` -> ``PRIOR_BLOCKED`` (a retry with identical
         inputs stays blocked; the prior blocked execution does not become
         eligible on retry without new valid inputs that change the
         fingerprint)
       - prior ``PARTIALLY_BLOCKED`` -> ``PRIOR_PARTIALLY_BLOCKED`` (remains
         review-required on retry)
       - prior ``UNSUPPORTED`` or ``CONFLICT`` -> ``PRIOR_REQUIRES_REVIEW``
         (remains review-required / safely blocked on retry)
    3. Prior execution with a **different** fingerprint -> ``CONFLICT``.
       Same idempotency key with materially different operation payload is
       rejected as a conflict, never silently accepted. (A prior ``CONFLICT``
       status with a different fingerprint is still ``CONFLICT`` here --
       the materially-different-payload rejection takes precedence.)

    The prior execution's audit reference (execution id, status, reason,
    fingerprint, executed_at) is always preserved on the classification
    when a prior execution exists, so callers can explain what happened and
    why without re-running the pipeline.

    Args:
        orch_input: Typed orchestration input whose plan content and
            idempotency key are being classified.
        repository: Caller-supplied durable persistence adapter. The
            repository owns its connection; this helper never opens a
            database. Must not be ``None`` -- idempotency classification is
            only meaningful against durable persisted state.

    Returns:
        An ``ApplyIdempotencyClassification`` carrying the outcome, the
        candidate fingerprint, and the prior execution audit reference.

    Raises:
        ValueError: When ``orch_input.idempotency_key`` is empty, when
            ``orch_input.plan_inputs`` is empty, or when ``repository`` is
            ``None``.
    """
    if repository is None:  # pragma: no cover - defensive, type system guards this
        raise ValueError("classify_reconciliation_apply_idempotency requires a repository")
    if not orch_input.idempotency_key:
        raise ValueError(
            "classify_reconciliation_apply_idempotency requires a non-empty idempotency_key"
        )
    if not orch_input.plan_inputs:
        raise ValueError(
            "classify_reconciliation_apply_idempotency requires at least one plan input"
        )

    # Build the same dry-run plan the orchestrator would build, so the
    # fingerprint is computed over identical content the runtime would see.
    plan = build_reconciliation_apply_plan(
        list(orch_input.plan_inputs),
        plan_label=orch_input.plan_label,
    )

    candidate_fingerprint = build_guarded_apply_execution_fingerprint(
        plan,
        orch_input.guard_decisions_by_operation_id,
        orch_input.idempotency_key,
    )

    prior_result = repository.get_by_idempotency_key(orch_input.idempotency_key)
    if prior_result is None:
        return ApplyIdempotencyClassification(
            outcome=ApplyIdempotencyOutcome.NO_PRIOR_EXECUTION,
            idempotency_key=orch_input.idempotency_key,
            candidate_fingerprint=candidate_fingerprint,
            prior_execution_exists=False,
            classification_note=_build_classification_note(
                ApplyIdempotencyOutcome.NO_PRIOR_EXECUTION,
                prior_status=None,
                key=orch_input.idempotency_key,
            ),
        )

    prior_fingerprint = repository.get_idempotency_fingerprint(orch_input.idempotency_key)
    prior_execution_id = _derive_prior_execution_id(
        prior_result.plan_id, orch_input.idempotency_key
    )
    prior_status = prior_result.execution_status

    if prior_fingerprint == candidate_fingerprint:
        outcome = _outcome_for_matching_fingerprint(prior_status)
    else:
        outcome = ApplyIdempotencyOutcome.CONFLICT

    return ApplyIdempotencyClassification(
        outcome=outcome,
        idempotency_key=orch_input.idempotency_key,
        candidate_fingerprint=candidate_fingerprint,
        prior_execution_exists=True,
        prior_execution_id=prior_execution_id,
        prior_execution_status=prior_status,
        prior_execution_fingerprint=prior_fingerprint,
        prior_block_reason=prior_result.block_reason,
        prior_executed_at=prior_result.executed_at,
        classification_note=_build_classification_note(
            outcome,
            prior_status=prior_status,
            key=orch_input.idempotency_key,
        ),
    )


def _outcome_for_matching_fingerprint(
    prior_status: ApplyExecutionStatus,
) -> ApplyIdempotencyOutcome:
    """Map a prior execution status to the repeat outcome when the candidate
    fingerprint matches the persisted fingerprint.

    Each prior execution class gets an explicit repeat behaviour. A prior
    ``EXECUTED`` is an idempotent replay of a completed execution; any other
    prior status is safely recognized as the same recorded execution whose
    non-completed outcome is preserved on retry (never upgraded to completed).
    """
    if prior_status == ApplyExecutionStatus.EXECUTED:
        return ApplyIdempotencyOutcome.IDEMPOTENT_REPLAY
    if prior_status == ApplyExecutionStatus.BLOCKED:
        return ApplyIdempotencyOutcome.PRIOR_BLOCKED
    if prior_status == ApplyExecutionStatus.PARTIALLY_BLOCKED:
        return ApplyIdempotencyOutcome.PRIOR_PARTIALLY_BLOCKED
    # UNSUPPORTED and a same-fingerprint prior CONFLICT both remain
    # review-required / safely blocked on retry.
    return ApplyIdempotencyOutcome.PRIOR_REQUIRES_REVIEW


def _build_classification_note(
    outcome: ApplyIdempotencyOutcome,
    *,
    prior_status: ApplyExecutionStatus | None,
    key: str,
) -> str:
    """Build a concise, deterministic human-readable classification note."""
    status_str = prior_status.value if prior_status is not None else "unknown"
    if outcome == ApplyIdempotencyOutcome.NO_PRIOR_EXECUTION:
        return (
            f"Idempotency classification for key '{key}': no prior execution. "
            f"A first apply would record the execution fingerprint normally. "
            f"Read-only classification -- no execution, no final financial records."
        )
    if outcome == ApplyIdempotencyOutcome.IDEMPOTENT_REPLAY:
        return (
            f"Idempotency classification for key '{key}': idempotent replay. "
            f"Prior execution status '{status_str}'. "
            f"A repeat apply would be recognized as the same recorded execution "
            f"without inserting a duplicate execution or operation row. "
            f"Read-only classification -- no final financial records."
        )
    if outcome == ApplyIdempotencyOutcome.CONFLICT:
        return (
            f"Idempotency classification for key '{key}': conflict. "
            f"Prior execution status '{status_str}'. "
            f"Same idempotency key with materially different plan content is "
            f"rejected as a conflict, never silently accepted. "
            f"Read-only classification -- no final financial records."
        )
    if outcome == ApplyIdempotencyOutcome.PRIOR_BLOCKED:
        return (
            f"Idempotency classification for key '{key}': prior blocked. "
            f"Prior execution status '{status_str}'. "
            f"A retry with identical inputs stays blocked; the prior blocked "
            f"execution does not become eligible without new valid inputs. "
            f"Read-only classification -- no final financial records."
        )
    if outcome == ApplyIdempotencyOutcome.PRIOR_PARTIALLY_BLOCKED:
        return (
            f"Idempotency classification for key '{key}': prior partially blocked. "
            f"Prior execution status '{status_str}'. "
            f"A retry remains review-required; the partially blocked outcome "
            f"is not converted into a completed result. "
            f"Read-only classification -- no final financial records."
        )
    # PRIOR_REQUIRES_REVIEW
    return (
        f"Idempotency classification for key '{key}': prior requires review. "
        f"Prior execution status '{status_str}'. "
        f"A retry remains review-required / safely blocked; the prior outcome "
        f"is not converted into a completed result. "
        f"Read-only classification -- no final financial records."
    )


def _derive_prior_execution_id(plan_id: str, idempotency_key: str) -> str:
    """Derive the prior execution id using the repository's stable scheme.

    Mirrors ``GuardedApplyExecutionRepository._derive_execution_id`` so the
    classification's ``prior_execution_id`` matches the persisted row's
    primary key without the helper reaching into repository internals.
    """
    import hashlib

    raw = f"{plan_id}|{idempotency_key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"exec-{digest[:16]}"


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------

__all__ = [
    "ApplyIdempotencyClassification",
    "ApplyIdempotencyOutcome",
    "ApplyOrchestrationInput",
    "ApplyOrchestrationResult",
    "ApplyOrchestrationStatus",
    "classify_reconciliation_apply_idempotency",
    "orchestrate_reconciliation_apply",
]
