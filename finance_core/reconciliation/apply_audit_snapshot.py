"""Reconciliation Apply Audit Snapshot v1 -- read-only audit snapshot layer
that explains a completed or attempted reconciliation apply run end to end.

The snapshot is for **audit/explanation only**. It never executes apply,
mutates final transactions, opens live databases, or creates settlement
obligations.

Key invariants:
- Read-only: no execution, no final transaction mutation, no live DB writes.
- Deterministic: stable snapshot id, stable operation ordering, no
  current-time dependency unless explicitly provided by caller, no random
  UUIDs.
- No write to ``database/finance.db``. The repository-based builder
  accepts caller-supplied repository objects only; it never opens a
  database.
- Boundaries preserved: a completed apply does not mean final financial
  write happened; dry-run guarded execution is not final mutation
  authorization; final write remains behind the separate guarded final
  mutation workflow; AI is not final monetary authority.
- No Telegram / OCR / PDF / Metabase / settlement scope.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from finance_core.reconciliation.models import (
    ApplyExecutionStatus,
)

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Frozen snapshot dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyAuditGuardSnapshot:
    """Immutable per-operation guard decision detail for audit traceability.

    Captures the guard decision that governed a single operation during
    the apply run. Every field is deterministic and derived from the
    ``FinalMutationGuardDecision`` that was passed to the runtime.
    """

    decision_id: str
    """The guard decision identifier (proposal_id)."""

    approved: bool
    """True when the guard approved the mutation proposal for this operation."""

    blocked_reasons: tuple[str, ...] = field(default_factory=tuple)
    """Guard blocking reasons when the decision was not approved. Empty when approved."""

    idempotency_key: str | None = None
    """Guard-level idempotency key, when available."""

    action: str = ""
    """The guard-evaluated action (e.g. NO_FINAL_MUTATION, CREATE_FINAL_TRANSACTION)."""


@dataclass(frozen=True)
class ApplyAuditOperationSnapshot:
    """Immutable per-operation audit snapshot capturing execution outcome
    and guard decision for a single operation within the apply run.

    Operation snapshots are sorted by ``operation_id`` for deterministic
    ordering.
    """

    operation_id: str
    """Stable operation identifier (e.g. ``op-dec-001``)."""

    decision_id: str
    """The source resolution decision identity."""

    mutation_type: str
    """The mutation action type (e.g. ``confirm_match``)."""

    execution_status: str
    """Per-operation execution status from the guarded runtime."""

    guard_approved: bool
    """True when the guard approved the mutation proposal for this operation."""

    guard_blocked_reasons: tuple[str, ...] = field(default_factory=tuple)
    """Guard blocking reasons when the guard was not approved."""

    guard_idempotency_key: str | None = None
    """Guard-level idempotency key, when available."""

    reason: str = ""
    """Human-readable reason for the operation outcome."""

    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    """Evidence public IDs for audit traceability."""


@dataclass(frozen=True)
class ApplyAuditIdempotencySnapshot:
    """Immutable audit snapshot of idempotency classification for a repeat
    apply attempt.

    Captures the outcome of ``classify_reconciliation_apply_idempotency``
    so the audit snapshot can explain whether the run was a first apply
    or a repeat, and what the prior execution state was. None when
    idempotency classification was not performed or not applicable.
    """

    idempotency_key: str
    """The idempotency key being classified."""

    outcome: str
    """Stable outcome string (e.g. ``idempotent_replay``, ``no_prior_execution``)."""

    candidate_fingerprint: str
    """The deterministic execution fingerprint of the supplied plan content."""

    prior_execution_exists: bool
    """True when a prior execution exists for the idempotency key."""

    prior_execution_id: str | None = None
    """The execution id of the prior execution, when one exists."""

    prior_execution_status: str | None = None
    """The execution status of the prior execution, when one exists."""

    prior_execution_fingerprint: str | None = None
    """The persisted execution fingerprint of the prior execution, when one exists."""

    fingerprint_matches_prior: bool = False
    """True when the candidate fingerprint matches the prior persisted fingerprint."""

    is_duplicate_prevention_rejection: bool = False
    """True when the repeat attempt was safely rejected as a duplicate."""

    is_completed_idempotent_replay: bool = False
    """True when the repeat is an idempotent replay of a completed execution."""

    classification_note: str = ""
    """Human-readable, deterministic classification explanation."""


@dataclass(frozen=True)
class ApplyAuditSnapshot:
    """Immutable end-to-end audit snapshot of a completed or attempted
    reconciliation apply run.

    Carries everything needed to explain:
    - what apply input/plan was used,
    - which guard decisions governed each operation,
    - what operations were planned,
    - what execution result was recorded,
    - which operations executed/blocked/skipped,
    - what idempotency key/fingerprint was involved,
    - whether the run was persisted,
    - why the run completed, blocked, partially blocked, conflicted,
      unsupported, or required review.

    The snapshot is read-only audit evidence. It does not authorize any
    final mutation and does not alter any financial records.
    """

    snapshot_id: str
    """Deterministic snapshot identifier derived from plan_id and
    idempotency_key. Stable across repeated builds for the same apply run."""

    plan_id: str
    """The dry-run apply plan identifier."""

    idempotency_key: str
    """The idempotency key used for guarded execution."""

    orchestration_status: str
    """Top-level orchestration status (e.g. ``completed``, ``blocked``)."""

    execution_status: str
    """Underlying guarded apply execution status (e.g. ``executed``)."""

    persisted: bool
    """True when the execution result was durably persisted via the repository."""

    persistence_enabled: bool
    """True when a repository was supplied for durable persistence."""

    review_required: bool
    """True when the execution result requires human review."""

    final_write_confirmation_required: bool | None = None
    """True when the adapter result says final write confirmation is
    required before any final financial record mutation. None when no
    adapter result was supplied."""

    candidate_fingerprint: str | None = None
    """The candidate execution fingerprint, when the idempotency
    classification provides one."""

    persisted_fingerprint: str | None = None
    """The persisted execution fingerprint from a prior execution.
    None when no prior execution exists."""

    total_operations: int = 0
    """Total number of operations in the apply plan."""

    executed_count: int = 0
    """Number of operations that executed (guard-cleared)."""

    blocked_count: int = 0
    """Number of operations blocked by the guard."""

    skipped_count: int = 0
    """Number of operations skipped (neither executed nor blocked)."""

    operation_snapshots: tuple[ApplyAuditOperationSnapshot, ...] = field(default_factory=tuple)
    """Per-operation audit snapshots in deterministic order."""

    guard_snapshots: tuple[ApplyAuditGuardSnapshot, ...] = field(default_factory=tuple)
    """Guard decision snapshots in deterministic order."""

    idempotency_snapshot: ApplyAuditIdempotencySnapshot | None = None
    """Idempotency classification snapshot, when available."""

    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    """Guard decision evidence references from the execution result."""

    audit_refs: tuple[str, ...] = field(default_factory=tuple)
    """Audit trail references. Includes the snapshot_id as a stable
    reference point."""

    human_explanation: str = ""
    """Concise, deterministic, human-readable explanation of the entire
    apply run suitable for audit review."""


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


def build_apply_audit_snapshot(
    orch_result,  # ApplyOrchestrationResult
    *,
    orch_input=None,  # ApplyOrchestrationInput | None
    adapter_result=None,  # FinalTransactionAdapterResult | None
    idempotency_classification=None,  # ApplyIdempotencyClassification | None
) -> ApplyAuditSnapshot:
    """Build a deterministic, read-only audit snapshot from an already-built
    in-memory apply orchestration result.

    The snapshot captures the complete apply run: what was planned, what
    guard decisions governed each operation, what execution outcome was
    recorded, which operations executed/blocked/skipped, what
    idempotency key/fingerprint was involved, whether the run was
    persisted, and why the run reached its final status.

    Args:
        orch_result: The orchestration result produced by
            ``orchestrate_reconciliation_apply()`` (required).
        orch_input: The original orchestration input, if available. When
            supplied, guard decision details are captured per operation.
        adapter_result: An optional ``FinalTransactionAdapterResult``
            from the final transaction adapter. When supplied, the
            ``final_write_confirmation_required`` flag is captured.
        idempotency_classification: An optional
            ``ApplyIdempotencyClassification`` from the idempotency
            classifier. When supplied, an idempotency snapshot sub-record
            is included.

    Returns:
        An ``ApplyAuditSnapshot`` with all fields populated from the
        supplied objects. The snapshot id is deterministic and stable
        for the same plan_id + idempotency_key combination.

    Never executes apply, mutates final transactions, opens a database,
    or creates settlement obligations.
    """
    plan = orch_result.plan
    exec_result = orch_result.execution_result

    snapshot_id = _derive_snapshot_id(plan.plan_id, orch_result.idempotency_key)

    # Operation snapshots -- sorted by operation_id for determinism
    op_snapshots = _build_operation_snapshots(exec_result, orch_input)
    guard_snapshots = _build_guard_snapshots(orch_input)

    # Idempotency sub-snapshot
    idem_snap = _build_idempotency_snapshot(idempotency_classification)

    # Evidence and audit refs
    evidence_refs = exec_result.guard_decision_refs
    audit_refs = (snapshot_id, orch_result.idempotency_key, plan.plan_id)

    # Determine final-write-confirmation-required
    final_write_required: bool | None = None
    if adapter_result is not None:
        final_write_required = adapter_result.requires_final_write_confirmation

    # Candidate and persisted fingerprints
    candidate_fp: str | None = None
    persisted_fp: str | None = None
    if idempotency_classification is not None:
        candidate_fp = idempotency_classification.candidate_fingerprint
        persisted_fp = idempotency_classification.prior_execution_fingerprint

    # Explanation
    explanation = _build_human_explanation(
        orch_result.status.value,
        exec_result.execution_status.value,
        orch_result.persisted,
        orch_result.persistence_enabled,
        exec_result.total_operations,
        exec_result.operated_executed,
        exec_result.operated_blocked,
        exec_result.operated_skipped,
        orch_result.review_summary.requires_human_review,
        final_write_required=final_write_required,
        idem_outcome=(
            idempotency_classification.outcome.value if idempotency_classification else None
        ),
    )

    return ApplyAuditSnapshot(
        snapshot_id=snapshot_id,
        plan_id=plan.plan_id,
        idempotency_key=orch_result.idempotency_key,
        orchestration_status=orch_result.status.value,
        execution_status=exec_result.execution_status.value,
        persisted=orch_result.persisted,
        persistence_enabled=orch_result.persistence_enabled,
        review_required=orch_result.review_summary.requires_human_review,
        final_write_confirmation_required=final_write_required,
        candidate_fingerprint=candidate_fp,
        persisted_fingerprint=persisted_fp,
        total_operations=exec_result.total_operations,
        executed_count=exec_result.operated_executed,
        blocked_count=exec_result.operated_blocked,
        skipped_count=exec_result.operated_skipped,
        operation_snapshots=op_snapshots,
        guard_snapshots=guard_snapshots,
        idempotency_snapshot=idem_snap,
        evidence_refs=evidence_refs,
        audit_refs=audit_refs,
        human_explanation=explanation,
    )


def build_apply_audit_snapshot_from_repository(
    orch_input,
    repository,  # GuardedApplyExecutionRepository
    *,
    adapter_result=None,  # FinalTransactionAdapterResult | None
) -> ApplyAuditSnapshot:
    """Build an audit snapshot by loading a persisted execution result from
    a caller-supplied repository.

    This is a **read-only** operation: it loads the persisted execution
    result for ``orch_input.idempotency_key`` (if one exists), re-runs
    the plan builder to produce the candidate fingerprint, and builds a
    snapshot from the combined in-memory and persisted state.

    It never:
    - opens ``database/finance.db``,
    - persists, updates, or deletes anything,
    - calls the guarded runtime or final mutation workflow,
    - mutates final financial records.

    When no prior execution exists for the idempotency key, the snapshot
    remains useful: it captures the plan, the input guard decisions, and
    a human-readable explanation that this is a first attempt with no
    prior recorded outcome.

    Args:
        orch_input: The apply orchestration input carrying plan inputs,
            guard decisions, and idempotency key.
        repository: Caller-supplied durable persistence adapter. The
            repository owns its connection; this function never opens a
            database.
        adapter_result: Optional ``FinalTransactionAdapterResult`` for
            capturing the final-write boundary.

    Returns:
        An ``ApplyAuditSnapshot`` built from the persisted execution
        result (when one exists) plus the in-memory plan and guard
        decisions.

    Raises:
        ValueError: When ``orch_input.idempotency_key`` is empty, when
            ``orch_input.plan_inputs`` is empty, or when ``repository``
            is ``None``.
    """
    from finance_core.reconciliation.apply_plan import build_reconciliation_apply_plan
    from finance_core.reconciliation.apply_runtime import build_guarded_apply_execution_fingerprint

    if repository is None:
        raise ValueError("build_apply_audit_snapshot_from_repository requires a repository")
    if not orch_input.idempotency_key:
        raise ValueError(
            "build_apply_audit_snapshot_from_repository requires a non-empty idempotency_key"
        )
    if not orch_input.plan_inputs:
        raise ValueError(
            "build_apply_audit_snapshot_from_repository requires at least one plan input"
        )

    plan = build_reconciliation_apply_plan(
        list(orch_input.plan_inputs),
        plan_label=orch_input.plan_label if hasattr(orch_input, "plan_label") else "",
    )
    candidate_fp = build_guarded_apply_execution_fingerprint(
        plan,
        orch_input.guard_decisions_by_operation_id,
        orch_input.idempotency_key,
    )

    persisted_result = repository.get_by_idempotency_key(orch_input.idempotency_key)
    persisted_fp = repository.get_idempotency_fingerprint(orch_input.idempotency_key)

    snapshot_id = _derive_snapshot_id(plan.plan_id, orch_input.idempotency_key)

    if persisted_result is not None:
        return _build_snapshot_from_persisted(
            snapshot_id=snapshot_id,
            plan=plan,
            idempotency_key=orch_input.idempotency_key,
            persisted_result=persisted_result,
            candidate_fp=candidate_fp,
            persisted_fp=persisted_fp,
            orch_input=orch_input,
            adapter_result=adapter_result,
            repository=repository,
        )

    # No prior execution: build a snapshot that explains the first attempt
    op_snapshots = _build_operation_snapshots_from_plan(plan, orch_input)
    guard_snapshots = _build_guard_snapshots(orch_input)

    explanation = _build_no_prior_explanation(
        plan.plan_id,
        orch_input.idempotency_key,
        plan.total_operations,
        plan.approved_operations,
        plan.blocked_operations,
        adapter_result,
    )

    return ApplyAuditSnapshot(
        snapshot_id=snapshot_id,
        plan_id=plan.plan_id,
        idempotency_key=orch_input.idempotency_key,
        orchestration_status="no_prior_execution",
        execution_status="no_prior_execution",
        persisted=False,
        persistence_enabled=True,
        review_required=True,
        final_write_confirmation_required=(
            adapter_result.requires_final_write_confirmation if adapter_result is not None else None
        ),
        candidate_fingerprint=candidate_fp,
        persisted_fingerprint=persisted_fp,
        total_operations=plan.total_operations,
        executed_count=0,
        blocked_count=plan.blocked_operations,
        skipped_count=0,
        operation_snapshots=op_snapshots,
        guard_snapshots=guard_snapshots,
        idempotency_snapshot=None,
        evidence_refs=(),
        audit_refs=(snapshot_id, orch_input.idempotency_key, plan.plan_id),
        human_explanation=explanation,
    )


# ---------------------------------------------------------------------------
# Internal helpers -- deterministic, no side effects
# ---------------------------------------------------------------------------


def _derive_snapshot_id(plan_id: str, idempotency_key: str) -> str:
    """Derive a deterministic, stable snapshot id from plan_id and idempotency_key."""
    raw = f"{plan_id}|{idempotency_key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"snap-{digest[:16]}"


def _build_operation_snapshots(exec_result, orch_input) -> tuple[ApplyAuditOperationSnapshot, ...]:
    """Build per-operation audit snapshots from the execution result,
    enriched with guard decision details when orch_input is available."""
    ops: list[ApplyAuditOperationSnapshot] = []

    for op_result in exec_result.results:
        guard_snap = _find_guard_snapshot(op_result.operation_id, orch_input)
        evidence_refs: tuple[str, ...] = ()
        if isinstance(op_result.mutation_payload, dict):
            raw_ev = op_result.mutation_payload.get("evidence_refs")
            if isinstance(raw_ev, (list, tuple)):
                evidence_refs = tuple(str(e) for e in raw_ev)

        ops.append(
            ApplyAuditOperationSnapshot(
                operation_id=op_result.operation_id,
                decision_id=op_result.decision_id,
                mutation_type=op_result.mutation_type,
                execution_status=op_result.execution_status,
                guard_approved=(
                    guard_snap.approved if guard_snap else op_result.guard_decision_approved
                ),
                guard_blocked_reasons=(
                    guard_snap.blocked_reasons if guard_snap else op_result.guard_blocked_reasons
                ),
                guard_idempotency_key=(
                    guard_snap.idempotency_key
                    if guard_snap
                    else op_result.guard_decision_idempotency_key
                ),
                reason=op_result.reason,
                evidence_refs=evidence_refs,
            )
        )

    # Deterministic ordering: sort by operation_id
    ops.sort(key=lambda o: o.operation_id)
    return tuple(ops)


def _build_guard_snapshots(orch_input) -> tuple[ApplyAuditGuardSnapshot, ...]:
    """Build guard decision audit snapshots from the orchestration input,
    sorted by decision_id for determinism.

    Sorting is keyed on ``decision_id`` so that the guard snapshot order is
    independent of the iteration order of the incoming dict keys.
    """
    if orch_input is None:
        return ()

    guards: list[ApplyAuditGuardSnapshot] = []
    for gd in sorted(
        orch_input.guard_decisions_by_operation_id.values(), key=lambda gd: gd.proposal_id
    ):
        guards.append(
            ApplyAuditGuardSnapshot(
                decision_id=gd.proposal_id,
                approved=gd.approved,
                blocked_reasons=gd.blocked_reasons,
                idempotency_key=None,
                action=gd.action.value,
            )
        )
    return tuple(guards)


def _find_guard_snapshot(operation_id: str, orch_input) -> ApplyAuditGuardSnapshot | None:
    """Find the guard snapshot for a given operation_id."""
    if orch_input is None:
        return None

    gd = orch_input.guard_decisions_by_operation_id.get(operation_id)
    if gd is None:
        return None

    return ApplyAuditGuardSnapshot(
        decision_id=gd.proposal_id,
        approved=gd.approved,
        blocked_reasons=gd.blocked_reasons,
        idempotency_key=None,
        action=gd.action.value,
    )


def _build_idempotency_snapshot(idempotency_classification) -> ApplyAuditIdempotencySnapshot | None:
    """Build an idempotency sub-snapshot from the classification."""
    if idempotency_classification is None:
        return None

    return ApplyAuditIdempotencySnapshot(
        idempotency_key=idempotency_classification.idempotency_key,
        outcome=idempotency_classification.outcome.value,
        candidate_fingerprint=idempotency_classification.candidate_fingerprint,
        prior_execution_exists=idempotency_classification.prior_execution_exists,
        prior_execution_id=idempotency_classification.prior_execution_id,
        prior_execution_status=(
            idempotency_classification.prior_execution_status.value
            if idempotency_classification.prior_execution_status is not None
            else None
        ),
        prior_execution_fingerprint=idempotency_classification.prior_execution_fingerprint,
        fingerprint_matches_prior=idempotency_classification.fingerprint_matches_prior,
        is_duplicate_prevention_rejection=idempotency_classification.is_duplicate_prevention_rejection,
        is_completed_idempotent_replay=idempotency_classification.is_completed_idempotent_replay,
        classification_note=idempotency_classification.classification_note,
    )


def _build_human_explanation(
    orch_status: str,
    exec_status: str,
    persisted: bool,
    persistence_enabled: bool,
    total: int,
    executed: int,
    blocked: int,
    skipped: int,
    review_required: bool,
    *,
    final_write_required: bool | None = None,
    idem_outcome: str | None = None,
) -> str:
    """Build a concise, deterministic human-readable explanation of the
    apply run suitable for audit review."""
    parts: list[str] = []

    # Status section
    if orch_status == "completed":
        parts.append(
            f"Apply run completed as dry-run: all {total} operation(s) were "
            f"guard-cleared and recorded. {executed} executed, {blocked} blocked, "
            f"{skipped} skipped."
        )
    elif orch_status == "blocked":
        parts.append(
            f"Apply run blocked: {executed} executed, {blocked} blocked, "
            f"{skipped} skipped. Reason: guard blocked all operations."
        )
    elif orch_status == "partially_blocked":
        parts.append(
            f"Apply run partially blocked: {executed} executed, {blocked} blocked, "
            f"{skipped} skipped. Some operations were guard-cleared and some blocked."
        )
    elif orch_status == "conflict":
        parts.append(
            f"Apply run conflicted: {executed} executed, {blocked} blocked, "
            f"{skipped} skipped. Idempotency key conflict detected."
        )
    elif orch_status == "unsupported":
        parts.append(
            f"Apply run unsupported: {total} operation(s) in plan but no operations "
            f"could be executed."
        )
    elif orch_status == "requires_review":
        parts.append(
            f"Apply run requires review: {executed} executed, {blocked} blocked, "
            f"{skipped} skipped. Human review is required before any action."
        )
    elif orch_status == "no_prior_execution":
        parts.append(
            f"Apply run has no prior execution: {total} operation(s) in plan. "
            f"A first apply would record the execution fingerprint normally."
        )
    else:
        parts.append(
            f"Apply run status '{orch_status}': {executed} executed, "
            f"{blocked} blocked, {skipped} skipped."
        )

    # Persistence section
    if persistence_enabled:
        if persisted:
            parts.append("Execution result was durably persisted.")
        else:
            parts.append(
                "Persistence was enabled but no new row was inserted "
                "(idempotent replay or conflict)."
            )
    else:
        parts.append("No persistence (dry-run / in-memory only).")

    # Idempotency section
    if idem_outcome is not None:
        parts.append(f"Idempotency outcome: {idem_outcome}.")

    # Review section
    if review_required:
        parts.append("Human review is required.")

    # Final-write boundary
    if final_write_required is True:
        parts.append(
            "Final write confirmation required: a separate human-confirmed "
            "final mutation step is needed before any final financial record "
            "is created or adjusted."
        )
    elif final_write_required is False:
        parts.append("Final write confirmation not required (ineligible path).")

    # Dry-run boundary
    parts.append("Guarded dry-run only -- no final financial records were created or modified.")

    return " ".join(parts)


def _build_snapshot_from_persisted(
    *,
    snapshot_id: str,
    plan,  # ReconciliationApplyPlan
    idempotency_key: str,
    persisted_result,  # GuardedApplyExecutionResult
    candidate_fp: str,
    persisted_fp: str | None,
    orch_input,  # ApplyOrchestrationInput | None
    adapter_result,  # FinalTransactionAdapterResult | None
    repository,  # GuardedApplyExecutionRepository
) -> ApplyAuditSnapshot:
    """Build a snapshot from a persisted execution result loaded via repository."""
    from finance_core.reconciliation.apply_execution_review import (
        build_guarded_apply_execution_review_summary,
    )

    review_summary = build_guarded_apply_execution_review_summary(persisted_result)
    op_snapshots = _build_operation_snapshots(persisted_result, orch_input)
    guard_snapshots = _build_guard_snapshots(orch_input)

    # Map execution status to a rough orchestration status for explanation
    exec_status_val = persisted_result.execution_status
    if exec_status_val == ApplyExecutionStatus.EXECUTED:
        orch_status_str = "completed"
    elif exec_status_val == ApplyExecutionStatus.BLOCKED:
        orch_status_str = "blocked"
    elif exec_status_val == ApplyExecutionStatus.PARTIALLY_BLOCKED:
        orch_status_str = "partially_blocked"
    elif exec_status_val == ApplyExecutionStatus.CONFLICT:
        orch_status_str = "conflict"
    elif exec_status_val == ApplyExecutionStatus.UNSUPPORTED:
        orch_status_str = "unsupported"
    else:
        orch_status_str = "requires_review"

    final_write_required: bool | None
    if adapter_result is not None:
        final_write_required = adapter_result.requires_final_write_confirmation
    else:
        final_write_required = None

    explanation = _build_human_explanation(
        orch_status_str,
        persisted_result.execution_status.value,
        True,  # persisted (loaded from repository)
        True,  # persistence_enabled
        persisted_result.total_operations,
        persisted_result.operated_executed,
        persisted_result.operated_blocked,
        persisted_result.operated_skipped,
        review_summary.requires_human_review,
        final_write_required=final_write_required,
    )

    return ApplyAuditSnapshot(
        snapshot_id=snapshot_id,
        plan_id=plan.plan_id,
        idempotency_key=idempotency_key,
        orchestration_status=orch_status_str,
        execution_status=persisted_result.execution_status.value,
        persisted=True,
        persistence_enabled=True,
        review_required=review_summary.requires_human_review,
        final_write_confirmation_required=final_write_required,
        candidate_fingerprint=candidate_fp,
        persisted_fingerprint=persisted_fp,
        total_operations=persisted_result.total_operations,
        executed_count=persisted_result.operated_executed,
        blocked_count=persisted_result.operated_blocked,
        skipped_count=persisted_result.operated_skipped,
        operation_snapshots=op_snapshots,
        guard_snapshots=guard_snapshots,
        idempotency_snapshot=None,
        evidence_refs=persisted_result.guard_decision_refs,
        audit_refs=(snapshot_id, idempotency_key, plan.plan_id),
        human_explanation=explanation,
    )


def _build_operation_snapshots_from_plan(
    plan,  # ReconciliationApplyPlan
    orch_input,  # ApplyOrchestrationInput | None
) -> tuple[ApplyAuditOperationSnapshot, ...]:
    """Build per-operation audit snapshots from a dry-run plan when no
    execution result is available (no prior execution path)."""

    ops: list[ApplyAuditOperationSnapshot] = []
    for op in plan.operations:
        guard_snap = _find_guard_snapshot(op.operation_id, orch_input)
        evidence_refs = op.evidence_refs if hasattr(op, "evidence_refs") else ()

        # Convert plan-level blocking flags to audit-only planned statuses
        if op.is_blocked:
            audit_status = "planned_guard_blocked"
        elif op.requires_human_confirmation and not op.guard_decision_approved:
            audit_status = "planned_requires_confirmation"
        else:
            audit_status = "planned_guard_approved"

        ops.append(
            ApplyAuditOperationSnapshot(
                operation_id=op.operation_id,
                decision_id=op.decision_id,
                mutation_type=op.action.value,
                execution_status=audit_status,
                guard_approved=(
                    guard_snap.approved
                    if guard_snap
                    else getattr(op, "guard_decision_approved", False)
                ),
                guard_blocked_reasons=(
                    guard_snap.blocked_reasons
                    if guard_snap
                    else getattr(op, "guard_blocked_reasons", ())
                ),
                guard_idempotency_key=guard_snap.idempotency_key if guard_snap else None,
                reason=op.dry_run_statement if hasattr(op, "dry_run_statement") else "",
                evidence_refs=evidence_refs,
            )
        )

    ops.sort(key=lambda o: o.operation_id)
    return tuple(ops)


def _build_no_prior_explanation(
    plan_id: str,
    idempotency_key: str,
    total: int,
    approved: int,
    blocked: int,
    adapter_result,  # FinalTransactionAdapterResult | None
) -> str:
    """Build explanation for a snapshot with no prior execution."""
    parts: list[str] = [
        f"Apply run has no prior persisted execution for key '{idempotency_key}'.",
        f"Plan '{plan_id}' has {total} operation(s): {approved} guard-approved, {blocked} blocked.",
    ]
    if adapter_result is not None:
        if adapter_result.is_eligible_for_final_mutation:
            parts.append(
                "Adapter says eligible for final mutation but "
                "final write confirmation is still required."
            )
        else:
            parts.append(f"Adapter says ineligible ({adapter_result.status.value}).")
    parts.append("No prior execution recorded -- a first apply would persist normally.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------

__all__ = [
    "ApplyAuditGuardSnapshot",
    "ApplyAuditIdempotencySnapshot",
    "ApplyAuditOperationSnapshot",
    "ApplyAuditSnapshot",
    "build_apply_audit_snapshot",
    "build_apply_audit_snapshot_from_repository",
]
