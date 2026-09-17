"""Reconciliation Apply Plan Builder v1 -- dry-run plan generation layer that
converts reconciliation review decisions and guard decisions into a structured
apply plan describing what a future final mutation would do.

This layer is intentionally **read-only**. It generates a plan describing what
would happen later, but never executes final transaction mutation, settlement
generation, calculation snapshot writes, or live database changes.

Key invariants:
- No final financial record mutation.
- No settlement obligation generation.
- No calculation snapshot writes.
- No reconciliation batch state mutation.
- Deterministic output: same inputs always produce the same plan.
- Plans marked as ``is_dry_run=True`` are explicitly not executed.
- Plans marked as blocked carry explicit blocking reasons from the guard.

Flow context::

    ResolutionDecision + ReviewQueueItem
           │
           ▼
    FinalMutationGuardDecision (from guard)
           │
           ▼
    ReconciliationApplyPlan  ←── this module
           │  ├── ApplyPlanOperation (per item)
           │  ├── ApplyPlanRisk (warnings)
           │  └── Summary (counts)
           │
           ▼
    Human Confirmation
           │
           ▼
    Future Final Mutation (not in this PR)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from finance_core.reconciliation.final_mutation_proposal import (
    FinalMutationGuardDecision,
    FinalMutationProposal,
)
from finance_core.reconciliation.models import (
    ResolutionAction,
    ResolutionDecision,
    ReviewQueueItem,
)

# ---------------------------------------------------------------------------
# Plan operation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyPlanOperation:
    """A single operation within a reconciliation apply plan.

    Describes what would happen during a future final mutation for one
    review queue item. This is a dry-run description only -- no mutation
    has been executed.
    """

    operation_id: str
    """Stable identifier for this operation within the plan."""

    decision_id: str
    """The source resolution decision identity."""

    queue_item_id: str
    """The review queue item this operation targets."""

    candidate_id: str
    """The reconciliation candidate identity."""

    action: ResolutionAction
    """The intended resolution action."""

    guard_decision_approved: bool
    """Whether the guard approved the final mutation proposal."""

    guard_blocked_reasons: tuple[str, ...] = field(default_factory=tuple)
    """Stable reason codes from the guard when blocked. Empty when approved."""

    is_blocked: bool = False
    """True when this operation must not proceed to final mutation."""

    requires_human_confirmation: bool = False
    """True when human confirmation is required before any future apply."""

    statement_ref: str | None = None
    """Statement row reference for traceability."""

    app_transaction_ref: str | None = None
    """App transaction reference for traceability."""

    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    """Evidence public IDs for audit traceability."""

    note: str = ""
    """Human-readable context from the resolution decision."""

    source_apply_result_ref: str | None = None
    """Link back to the source ResolutionApplyResult, if available."""

    dry_run_statement: str = (
        "Dry-run plan only. No final financial records were created or modified."
    )
    """Explicit confirmation that this operation is a plan, not a mutation."""


# ---------------------------------------------------------------------------
# Plan risk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyPlanRisk:
    """A risk or warning associated with an apply plan operation.

    Risks are informational only -- they do not block execution but
    should be reviewed before human confirmation.
    """

    risk_code: str
    """Stable risk code for tooling / classification."""

    risk_message: str
    """Human-readable risk description."""

    operation_id: str | None = None
    """The operation this risk applies to. None for plan-level risks."""

    severity: str = "warning"
    """Severity tier: 'critical', 'warning', or 'info'."""


# ---------------------------------------------------------------------------
# Apply plan input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyPlanInput:
    """Structured input for building a reconciliation apply plan.

    Carries the review decision, review queue item, and optional guard
    decision context. The builder uses this DTO to assemble a plan
    deterministically without depending on AI free-text.
    """

    decision: ResolutionDecision
    """The human reviewer's resolution decision."""

    queue_item: ReviewQueueItem
    """The review queue item being resolved."""

    guard_decision: FinalMutationGuardDecision | None = None
    """The guard's decision, if already evaluated. When absent, the
    plan builder treats the operation as requiring guard evaluation
    before any final mutation."""

    proposal: FinalMutationProposal | None = None
    """The final mutation proposal, if one was constructed from the
    resolution apply result. Used to enrich evidence references."""

    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    """Evidence public IDs for audit traceability."""


# ---------------------------------------------------------------------------
# Reconciliation apply plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconciliationApplyPlan:
    """A dry-run reconciliation apply plan describing what a future final
    mutation step would do.

    This plan is generated deterministically from structured inputs and
    carries explicit ``is_dry_run=True``. It must not be treated as an
    executed mutation.

    Plans marked ``requires_human_confirmation=True`` need explicit human
    approval before any future apply step. Plans with
    ``blocked_operations > 0`` carry operations that the guard explicitly
    blocked -- these must not proceed to final mutation.

    Fields
    ------
    plan_id:
        Stable plan identifier (deterministic from inputs).
    operations:
        Individual operations in the plan, one per input pair.
    risks:
        Plan-level and operation-level risks for review.
    requires_human_confirmation:
        True when the plan contains any operation that requires human
        confirmation before mutation.
    is_dry_run:
        Always True -- confirms no mutation has been executed.
    total_operations:
        Total number of operations in the plan.
    blocked_operations:
        Number of operations that are blocked by the guard.
    approved_operations:
        Number of operations whose guard decision approved the proposal.
    pending_guard_operations:
        Number of operations awaiting guard evaluation.
    note:
        Human-readable plan summary.
    """

    plan_id: str
    operations: tuple[ApplyPlanOperation, ...]
    risks: tuple[ApplyPlanRisk, ...]
    requires_human_confirmation: bool
    is_dry_run: bool
    total_operations: int
    blocked_operations: int
    approved_operations: int
    pending_guard_operations: int
    note: str


# ---------------------------------------------------------------------------
# Builder function
# ---------------------------------------------------------------------------


def build_reconciliation_apply_plan(
    inputs: list[ApplyPlanInput],
    *,
    plan_label: str = "",
) -> ReconciliationApplyPlan:
    """Build a deterministic dry-run reconciliation apply plan from structured
    review, guard, and evidence inputs.

    Each ``ApplyPlanInput`` produces one ``ApplyPlanOperation`` in the
    resulting plan. The builder:

    - Extracts decision identity, candidate identity, and action from
      each input's ``ResolutionDecision`` and ``ReviewQueueItem``.
    - Evaluates guard status from ``FinalMutationGuardDecision`` when
      available, or marks the operation as pending guard evaluation.
    - Marks operations as blocked when the guard decision explicitly
      blocks the proposal.
    - Marks operations as requiring human confirmation before any
      future mutation apply step.
    - Preserves evidence references from proposals and caller-supplied
      evidence refs.
    - Generates plan-level and operation-level risks.
    - Never mutates final financial records or reconciliation state.

    Returns:
        A ``ReconciliationApplyPlan`` with ``is_dry_run=True`` that
        describes what a future final mutation step would do.

    Raises:
        ValueError: When ``inputs`` is empty.
    """
    if not inputs:
        raise ValueError("build_reconciliation_apply_plan requires at least one input")

    operations: list[ApplyPlanOperation] = []
    risks: list[ApplyPlanRisk] = []

    for inp in inputs:
        op, op_risks = _build_operation(inp)
        operations.append(op)
        risks.extend(op_risks)

    # Compute summary counts
    total = len(operations)
    blocked = sum(1 for o in operations if o.is_blocked)
    approved = sum(1 for o in operations if o.guard_decision_approved and not o.is_blocked)
    pending_guard = sum(1 for o in operations if not o.guard_decision_approved and not o.is_blocked)

    # Build plan-level risk: high blocked ratio
    if total > 0 and blocked > 0:
        if blocked == total:
            risks.append(
                ApplyPlanRisk(
                    risk_code="all_operations_blocked",
                    risk_message=(
                        f"All {total} operations are blocked. "
                        "No final mutation should proceed without resolution."
                    ),
                    severity="critical",
                )
            )
        elif blocked > total // 2:
            risks.append(
                ApplyPlanRisk(
                    risk_code="majority_operations_blocked",
                    risk_message=(
                        f"{blocked} of {total} operations are blocked "
                        f"({blocked / total:.0%}). Review blocked reasons."
                    ),
                    severity="warning",
                )
            )

    # Build plan-level risk: pending guard evaluations
    if pending_guard > 0:
        risks.append(
            ApplyPlanRisk(
                risk_code="pending_guard_evaluations",
                risk_message=(
                    f"{pending_guard} of {total} operations await guard evaluation. "
                    "These must be evaluated before any final mutation."
                ),
                severity="warning" if pending_guard == total else "info",
            )
        )

    # Build plan-level risk: no evidence refs
    ops_without_evidence = sum(1 for o in operations if not o.evidence_refs)
    if ops_without_evidence > 0:
        risks.append(
            ApplyPlanRisk(
                risk_code="missing_evidence_refs",
                risk_message=(
                    f"{ops_without_evidence} of {total} operations have no "
                    "evidence references. Audit traceability may be reduced."
                ),
                severity="warning",
            )
        )

    # Deterministic plan ID
    plan_id = _build_plan_id(operations, plan_label)

    # Human confirmation required if any operation requires it
    requires_confirmation = any(o.requires_human_confirmation for o in operations)

    # Plan note
    if plan_label:
        note = (
            f"Apply plan '{plan_label}': {total} operation(s), "
            f"{approved} approved, {blocked} blocked, "
            f"{pending_guard} pending guard. "
            f"Dry-run only -- no final financial records have been created or modified."
        )
    else:
        note = (
            f"Apply plan: {total} operation(s), "
            f"{approved} approved, {blocked} blocked, "
            f"{pending_guard} pending guard. "
            f"Dry-run only -- no final financial records have been created or modified."
        )

    return ReconciliationApplyPlan(
        plan_id=plan_id,
        operations=tuple(operations),
        risks=tuple(risks),
        requires_human_confirmation=requires_confirmation,
        is_dry_run=True,
        total_operations=total,
        blocked_operations=blocked,
        approved_operations=approved,
        pending_guard_operations=pending_guard,
        note=note,
    )


# ---------------------------------------------------------------------------
# Internal -- operation builder
# ---------------------------------------------------------------------------


def _build_operation(inp: ApplyPlanInput) -> tuple[ApplyPlanOperation, list[ApplyPlanRisk]]:
    """Build a single operation and its associated risks from an ApplyPlanInput."""
    risks: list[ApplyPlanRisk] = []
    decision = inp.decision
    item = inp.queue_item
    cand = item.candidate

    # Extract references from the queue item candidate
    statement_ref = _statement_ref_from_item(item)
    app_transaction_ref = _app_ref_from_item(item)

    # Collect evidence refs: proposal refs take priority, then caller-supplied
    evidence_refs = inp.evidence_refs
    if inp.proposal is not None and inp.proposal.evidence_refs:
        evidence_refs = inp.proposal.evidence_refs

    operation_id = f"op-{decision.decision_id}"

    # Determine guard status
    guard_decision = inp.guard_decision
    guard_approved = False
    blocked_reasons: tuple[str, ...] = ()
    is_blocked = False

    if guard_decision is not None:
        guard_approved = guard_decision.approved
        blocked_reasons = guard_decision.blocked_reasons
        is_blocked = not guard_decision.approved

        # Risk: blocked operation
        if is_blocked:
            reasons_str = ", ".join(blocked_reasons)
            risks.append(
                ApplyPlanRisk(
                    risk_code="guard_blocked_operation",
                    risk_message=(f"Operation '{operation_id}' is blocked by guard: {reasons_str}"),
                    operation_id=operation_id,
                    severity="critical",
                )
            )

        # Risk: approved operation with no evidence refs
        if guard_approved and not evidence_refs:
            risks.append(
                ApplyPlanRisk(
                    risk_code="approved_operation_missing_evidence",
                    risk_message=(
                        f"Operation '{operation_id}' is approved but has no evidence "
                        "references. Human confirmation should verify evidence exists."
                    ),
                    operation_id=operation_id,
                    severity="warning",
                )
            )
    else:
        # No guard decision: mark as pending guard evaluation
        risks.append(
            ApplyPlanRisk(
                risk_code="pending_guard_evaluation",
                risk_message=(
                    f"Operation '{operation_id}' has no guard decision. "
                    "Guard evaluation is required before any final mutation."
                ),
                operation_id=operation_id,
                severity="warning",
            )
        )

    # Human confirmation is required for:
    # - Approved operations (someone must confirm before mutation)
    # - Pending guard operations (guard must evaluate first, then human confirms)
    # - Blocked operations do NOT require human confirmation (they are blocked)
    requires_confirmation = guard_approved or guard_decision is None

    # Risk: human confirmation required
    if requires_confirmation and not is_blocked:
        risks.append(
            ApplyPlanRisk(
                risk_code="human_confirmation_required",
                risk_message=(
                    f"Operation '{operation_id}' requires human confirmation "
                    "before any final mutation step."
                ),
                operation_id=operation_id,
                severity="info",
            )
        )

    # Build the operation
    op = ApplyPlanOperation(
        operation_id=operation_id,
        decision_id=decision.decision_id,
        queue_item_id=item.queue_item_id,
        candidate_id=cand.candidate_id,
        action=decision.action,
        guard_decision_approved=guard_approved,
        guard_blocked_reasons=blocked_reasons,
        is_blocked=is_blocked,
        requires_human_confirmation=requires_confirmation,
        statement_ref=statement_ref,
        app_transaction_ref=app_transaction_ref,
        evidence_refs=evidence_refs,
        note=decision.note,
        source_apply_result_ref=(inp.proposal.proposal_id if inp.proposal is not None else None),
    )

    return op, risks


# ---------------------------------------------------------------------------
# Plan ID builder (deterministic)
# ---------------------------------------------------------------------------


def _build_plan_id(
    operations: list[ApplyPlanOperation],
    plan_label: str,
) -> str:
    """Build a deterministic plan ID from the operations and optional label."""
    parts: list[str] = ["recon-apply-plan"]

    if plan_label:
        # Sanitize: replace non-alphanumeric with hyphens
        slug = "".join(c if c.isalnum() else "-" for c in plan_label.lower())
        slug = slug.strip("-")[:40]
        if slug:
            parts.append(slug)

    # Build a canonical fingerprint from all material operation fields.
    # Sort operations by identity fields for deterministic ordering.
    sorted_ops = sorted(
        operations,
        key=lambda o: (o.operation_id, o.decision_id, o.queue_item_id, o.candidate_id),
    )
    op_dicts: list[dict[str, object]] = []
    for o in sorted_ops:
        op_dicts.append(
            {
                "operation_id": o.operation_id,
                "decision_id": o.decision_id,
                "queue_item_id": o.queue_item_id,
                "candidate_id": o.candidate_id,
                "action": o.action.value,
                "guard_decision_approved": o.guard_decision_approved,
                "guard_blocked_reasons": sorted(o.guard_blocked_reasons),
                "is_blocked": o.is_blocked,
                "requires_human_confirmation": o.requires_human_confirmation,
                "statement_ref": o.statement_ref,
                "app_transaction_ref": o.app_transaction_ref,
                "evidence_refs": sorted(o.evidence_refs),
                "source_apply_result_ref": o.source_apply_result_ref,
            }
        )
    canonical = json.dumps(op_dicts, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    parts.append(digest)

    return "-".join(parts)


# ---------------------------------------------------------------------------
# Reference extractors
# ---------------------------------------------------------------------------


def _statement_ref_from_item(item: ReviewQueueItem) -> str | None:
    """Extract a stable statement reference from a review queue item."""
    stmt = item.candidate.statement
    if stmt is None:
        return None
    return stmt.statement_row_reference or stmt.merchant_raw


def _app_ref_from_item(item: ReviewQueueItem) -> str | None:
    """Extract a stable app transaction reference from a review queue item."""
    app = item.candidate.best_app_transaction
    if app is None:
        return None
    return app.app_txn_id


__all__ = [
    "ApplyPlanInput",
    "ApplyPlanOperation",
    "ApplyPlanRisk",
    "ReconciliationApplyPlan",
    "build_reconciliation_apply_plan",
]
