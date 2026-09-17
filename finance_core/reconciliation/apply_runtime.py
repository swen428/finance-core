"""Guarded Reconciliation Apply Runtime v1 -- bounded execution layer that
accepts a ``ReconciliationApplyPlan``, evaluates every operation against its
``FinalMutationGuardDecision``, enforces idempotency, and produces a
structured ``GuardedApplyExecutionResult``.

This runtime is the control point between the plan layer (dry-run preview)
and any future final transaction mutation.  In v1 it intentionally does
**not** execute final writes, settlement generation, Telegram replies, or
PDF imports.  It validates, blocks, and routes every operation through an
explicit guard decision gate, and records auditable results.

Key invariants:
- No final financial record mutation in v1.
- No settlement obligation generation.
- No Telegram / OCR / PDF / Metabase interaction.
- No write to ``database/finance.db``.
- Deterministic: same plan + same guard decisions + same idempotency key
  always produces the same execution result.
- Idempotent: replaying the same approved plan with the same key is safe.
- Conflicting replay with different content fails safely.
- Only supported, approved, guard-cleared operations may produce an
  ``EXECUTED`` status.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from finance_core.reconciliation.apply_plan import (
    ApplyPlanOperation,
    ReconciliationApplyPlan,
)
from finance_core.reconciliation.final_mutation_proposal import FinalMutationGuardDecision
from finance_core.reconciliation.models import (
    ApplyExecutionStatus,
    GuardedApplyExecutionResult,
    GuardedOperationResult,
)

if TYPE_CHECKING:
    from finance_core.reconciliation.apply_runtime_persistence import (
        GuardedApplyExecutionRepository,
    )

# ---------------------------------------------------------------------------
# Guarded apply runtime conflict error
# ---------------------------------------------------------------------------


class GuardedApplyRuntimeError(Exception):
    """Raised when the guarded runtime encounters a conflict or safety violation."""


# ---------------------------------------------------------------------------
# Supported mutation types for v1 execution
# ---------------------------------------------------------------------------

_V1_SUPPORTED_EXECUTION_ACTIONS: frozenset[str] = frozenset(
    {
        "confirm_match",
        "mark_duplicate",
        "mark_statement_only",
        "ignore",
    }
)


class GuardedApplyRuntime:
    """Guarded reconciliation apply runtime v1.

    Accepts a ``ReconciliationApplyPlan`` and a lookup of per-operation
    ``FinalMutationGuardDecision`` objects, evaluates every operation
    against the guard, enforces idempotency, and returns a structured
    ``GuardedApplyExecutionResult``.

    When a ``repository`` is provided, durable idempotency is checked
    before the in-memory cache and execution results are persisted after
    first execution. This enables idempotency to survive process
    restarts. When no repository is provided, behaviour is unchanged
    from the original in-memory-only implementation.
    """

    def __init__(
        self,
        repository: GuardedApplyExecutionRepository | None = None,
    ) -> None:
        self._repository = repository
        # In-memory idempotency: (plan_id, fingerprint, result)
        self._executed: dict[str, tuple[str, str, GuardedApplyExecutionResult]] = {}

    @property
    def has_persistence(self) -> bool:
        """Return True when a repository is configured for durable storage."""
        return self._repository is not None

    def execute(
        self,
        plan: ReconciliationApplyPlan,
        *,
        guard_decisions_by_operation_id: dict[str, FinalMutationGuardDecision],
        idempotency_key: str,
        clock: Callable[[], str] | None = None,
    ) -> GuardedApplyExecutionResult:
        """Execute a reconciliation apply plan through the guarded boundary."""
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")

        if not plan.operations:
            return self._build_result(
                plan=plan,
                idempotency_key=idempotency_key,
                status=ApplyExecutionStatus.UNSUPPORTED,
                results=(),
                block_reason="Plan contains no operations to execute.",
                clock=clock,
            )

        # Build canonical fingerprint for idempotency.
        # Must be done before any early-return paths so every execution
        # result (including missing-guard blocked results) can be
        # persisted with a consistent fingerprint.
        fingerprint = _build_execution_fingerprint(
            plan, guard_decisions_by_operation_id, idempotency_key
        )

        # Idempotency check (durable then in-memory).
        idem_result = self._check_durable_idempotency(plan, idempotency_key, fingerprint, clock)
        if idem_result is not None:
            return idem_result

        idem_result = self._check_idempotency(plan, idempotency_key, fingerprint, clock)
        if idem_result is not None:
            return idem_result

        # Check that every operation has a guard decision.
        # The blocked result is persisted before returning, so missing-
        # guard results are durably recorded for audit completeness.
        missing = _missing_guard_ops(plan, guard_decisions_by_operation_id)
        if missing:
            block_reason = (
                f"Missing guard decisions for operation(s): {', '.join(sorted(missing))}. "
                "Every operation in the apply plan must have a corresponding guard decision."
            )
            results = tuple(
                self._build_blocked_op_result(op, block_reason) for op in plan.operations
            )
            result = self._build_result(
                plan=plan,
                idempotency_key=idempotency_key,
                status=ApplyExecutionStatus.BLOCKED,
                results=results,
                block_reason=block_reason,
                clock=clock,
            )
            self._executed[idempotency_key] = (plan.plan_id, fingerprint, result)
            self._persist_if_configured(result, fingerprint)
            return result
        # Evaluate every operation through the guard
        per_op_results: list[GuardedOperationResult] = []
        executed = 0
        blocked = 0
        skipped = 0

        for op in plan.operations:
            gd = guard_decisions_by_operation_id[op.operation_id]
            op_result = _evaluate_operation(op, gd)
            per_op_results.append(op_result)
            if op_result.execution_status == ApplyExecutionStatus.EXECUTED:
                executed += 1
            elif op_result.execution_status == ApplyExecutionStatus.BLOCKED:
                blocked += 1
            else:
                skipped += 1

        # Determine plan-level status
        if blocked > 0 and executed > 0:
            plan_status = ApplyExecutionStatus.PARTIALLY_BLOCKED
        elif blocked > 0 or executed == 0:
            plan_status = ApplyExecutionStatus.BLOCKED
        else:
            plan_status = ApplyExecutionStatus.EXECUTED

        guard_refs = tuple(
            guard_decisions_by_operation_id[op.operation_id].proposal_id
            for op in plan.operations
            if op.operation_id in guard_decisions_by_operation_id
        )

        result = self._build_result(
            plan=plan,
            idempotency_key=idempotency_key,
            status=plan_status,
            results=tuple(per_op_results),
            block_reason=(
                _build_block_reason(per_op_results)
                if plan_status != ApplyExecutionStatus.EXECUTED
                else ""
            ),
            guard_refs=guard_refs,
            clock=clock,
        )

        self._executed[idempotency_key] = (plan.plan_id, fingerprint, result)
        self._persist_if_configured(result, fingerprint)
        return result

    def _check_idempotency(
        self,
        plan: ReconciliationApplyPlan,
        idempotency_key: str,
        fingerprint: str,
        clock: Callable[[], str] | None,
    ) -> GuardedApplyExecutionResult | None:
        """Return cached result on replay, CONFLICT on mismatch, or None."""
        if idempotency_key not in self._executed:
            return None

        prev_plan_id, prev_fingerprint, prev_result = self._executed[idempotency_key]

        if prev_plan_id != plan.plan_id:
            return self._build_result(
                plan=plan,
                idempotency_key=idempotency_key,
                status=ApplyExecutionStatus.CONFLICT,
                results=tuple(
                    self._build_blocked_op_result(
                        op,
                        f"Idempotency key '{idempotency_key}' already used for plan "
                        f"'{prev_plan_id}'. Same key with different plan is a conflict.",
                    )
                    for op in plan.operations
                ),
                block_reason=(
                    f"Idempotency key '{idempotency_key}' already used for plan "
                    f"'{prev_plan_id}'. Reusing the same key with a different plan "
                    f"('{plan.plan_id}') is a conflict."
                ),
                clock=clock,
            )

        if prev_fingerprint != fingerprint:
            return self._build_result(
                plan=plan,
                idempotency_key=idempotency_key,
                status=ApplyExecutionStatus.CONFLICT,
                results=tuple(
                    self._build_blocked_op_result(
                        op,
                        f"Idempotency key '{idempotency_key}' reused with different "
                        f"plan content (fingerprint mismatch).",
                    )
                    for op in plan.operations
                ),
                block_reason=(
                    f"Idempotency key '{idempotency_key}' reused with materially "
                    f"different plan content (fingerprint mismatch)."
                ),
                clock=clock,
            )

        return prev_result

    def _check_durable_idempotency(
        self,
        plan: ReconciliationApplyPlan,
        idempotency_key: str,
        fingerprint: str,
        clock: Callable[[], str] | None,
    ) -> GuardedApplyExecutionResult | None:
        """Check the durable repository for cached result or conflict."""
        if self._repository is None:
            return None

        cached = self._repository.get_by_idempotency_key(idempotency_key)
        if cached is None:
            return None

        stored_fp = self._repository.get_idempotency_fingerprint(idempotency_key)

        # Same key + same fingerprint -> cached result (idempotent replay)
        if stored_fp is not None and stored_fp == fingerprint:
            # Also warm the in-memory cache for future fast lookups
            self._executed[idempotency_key] = (
                cached.plan_id,
                fingerprint,
                cached,
            )
            return cached

        # Same key + different fingerprint -> conflict
        return self._build_result(
            plan=plan,
            idempotency_key=idempotency_key,
            status=ApplyExecutionStatus.CONFLICT,
            results=tuple(
                self._build_blocked_op_result(
                    op,
                    f"Idempotency key '{idempotency_key}' reused with different "
                    f"plan content (fingerprint mismatch against persisted state).",
                )
                for op in plan.operations
            ),
            block_reason=(
                f"Idempotency key '{idempotency_key}' reused with materially "
                f"different plan content (fingerprint mismatch against persisted state)."
            ),
            clock=clock,
        )

    @staticmethod
    def _build_blocked_op_result(
        op: ApplyPlanOperation,
        reason: str,
    ) -> GuardedOperationResult:
        return GuardedOperationResult(
            operation_id=op.operation_id,
            decision_id=op.decision_id,
            execution_status=ApplyExecutionStatus.BLOCKED,
            reason=reason,
            guard_decision_approved=False,
            mutation_type=op.action.value,
            mutation_payload=_op_payload(op),
        )

    @staticmethod
    def _build_result(
        *,
        plan: ReconciliationApplyPlan,
        idempotency_key: str,
        status: ApplyExecutionStatus,
        results: tuple[GuardedOperationResult, ...],
        block_reason: str = "",
        guard_refs: tuple[str, ...] = (),
        clock: Callable[[], str] | None = None,
    ) -> GuardedApplyExecutionResult:
        total = len(results)
        e = sum(1 for r in results if r.execution_status == ApplyExecutionStatus.EXECUTED)
        b = sum(1 for r in results if r.execution_status == ApplyExecutionStatus.BLOCKED)
        s = total - e - b

        executed_at = clock() if clock is not None else datetime.now(timezone.utc).isoformat()

        audit: dict[str, object] = {
            "plan_id": plan.plan_id,
            "idempotency_key": idempotency_key,
            "execution_status": status.value,
            "total_operations": total,
            "operations_executed": e,
            "operations_blocked": b,
            "operations_skipped": s,
            "block_reason": block_reason,
            "guard_decision_refs": list(guard_refs),
            "runtime_version": "v1",
            "is_dry_run": True,
            "dry_run_note": (
                "v1 guarded apply runtime: guard evaluation and result recording only. "
                "No final financial records were created or modified."
            ),
        }

        return GuardedApplyExecutionResult(
            plan_id=plan.plan_id,
            idempotency_key=idempotency_key,
            execution_status=status,
            results=results,
            total_operations=total,
            operated_executed=e,
            operated_blocked=b,
            operated_skipped=s,
            block_reason=block_reason,
            guard_decision_refs=guard_refs,
            executed_at=executed_at,
            audit_trail=audit,
            is_dry_run=True,
        )

    def _persist_if_configured(
        self,
        result: GuardedApplyExecutionResult,
        fingerprint: str,
    ) -> None:
        if self._repository is not None:
            self._repository.save_execution_result(result, execution_fingerprint=fingerprint)

    def reset(self) -> None:
        self._executed.clear()

    def is_executed(self, idempotency_key: str) -> bool:
        if idempotency_key in self._executed:
            return True
        if self._repository is not None:
            return self._repository.has_idempotency_key(idempotency_key)
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _missing_guard_ops(
    plan: ReconciliationApplyPlan,
    guard_decisions: dict[str, FinalMutationGuardDecision],
) -> set[str]:
    plan_ids = {op.operation_id for op in plan.operations}
    guard_ids = set(guard_decisions.keys())
    return plan_ids - guard_ids


def _evaluate_operation(
    op: ApplyPlanOperation,
    gd: FinalMutationGuardDecision,
) -> GuardedOperationResult:
    """Evaluate a single plan operation against its guard decision."""
    action_value = op.action.value

    # Check: supported mutation type
    if action_value not in _V1_SUPPORTED_EXECUTION_ACTIONS:
        return GuardedOperationResult(
            operation_id=op.operation_id,
            decision_id=op.decision_id,
            execution_status=ApplyExecutionStatus.BLOCKED,
            reason=(
                f"Operation '{op.operation_id}' action '{action_value}' is not a "
                f"supported execution type in v1. Supported: "
                f"{', '.join(sorted(_V1_SUPPORTED_EXECUTION_ACTIONS))}."
            ),
            guard_decision_approved=gd.approved,
            guard_decision_idempotency_key=None,
            guard_blocked_reasons=(),
            mutation_type=action_value,
            mutation_payload=_op_payload(op),
        )

    # Check: guard decision approved
    if not gd.approved:
        return GuardedOperationResult(
            operation_id=op.operation_id,
            decision_id=op.decision_id,
            execution_status=ApplyExecutionStatus.BLOCKED,
            reason=(
                f"Guard decision for operation '{op.operation_id}' is not approved. "
                f"Blocked reasons: {', '.join(gd.blocked_reasons) or 'unspecified'}."
            ),
            guard_decision_approved=False,
            guard_decision_idempotency_key=None,
            guard_blocked_reasons=gd.blocked_reasons,
            mutation_type=action_value,
            mutation_payload=_op_payload(op),
        )

    # Check: guard decision proposal_id matches operation ref
    if op.source_apply_result_ref is not None and gd.proposal_id != op.source_apply_result_ref:
        return GuardedOperationResult(
            operation_id=op.operation_id,
            decision_id=op.decision_id,
            execution_status=ApplyExecutionStatus.BLOCKED,
            reason=(
                f"Guard decision proposal_id '{gd.proposal_id}' does not match "
                f"operation source_apply_result_ref '{op.source_apply_result_ref}'. "
                f"Cross-wiring guard decisions is unsafe."
            ),
            guard_decision_approved=gd.approved,
            guard_decision_idempotency_key=None,
            guard_blocked_reasons=(),
            mutation_type=action_value,
            mutation_payload=_op_payload(op),
        )

    # All checks passed
    return GuardedOperationResult(
        operation_id=op.operation_id,
        decision_id=op.decision_id,
        execution_status=ApplyExecutionStatus.EXECUTED,
        reason=(
            f"Operation '{op.operation_id}' executed successfully through guarded apply"
            " runtime v1. Dry-run only -- no final financial records were created or"
            " modified."
        ),
        guard_decision_approved=True,
        guard_decision_idempotency_key=None,
        guard_blocked_reasons=(),
        mutation_type=action_value,
        mutation_payload=_op_payload(op),
    )


def _op_payload(op: ApplyPlanOperation) -> dict[str, object]:
    """Extract a minimal audit payload from an apply plan operation."""
    payload: dict[str, object] = {
        "operation_id": op.operation_id,
        "decision_id": op.decision_id,
        "queue_item_id": op.queue_item_id,
        "candidate_id": op.candidate_id,
        "action": op.action.value,
        "guard_decision_approved": op.guard_decision_approved,
        "is_blocked": op.is_blocked,
        "requires_human_confirmation": op.requires_human_confirmation,
        "statement_ref": op.statement_ref,
        "app_transaction_ref": op.app_transaction_ref,
        "evidence_refs": list(op.evidence_refs),
        "note": op.note,
    }
    if op.source_apply_result_ref:
        payload["source_apply_result_ref"] = op.source_apply_result_ref
    if op.guard_blocked_reasons:
        payload["guard_blocked_reasons"] = list(op.guard_blocked_reasons)
    return payload


def _build_execution_fingerprint(
    plan: ReconciliationApplyPlan,
    guard_decisions: dict[str, FinalMutationGuardDecision],
    idempotency_key: str,
) -> str:
    """Build a deterministic SHA-256 fingerprint of the execution context."""
    ops_canonical: list[dict[str, object]] = []
    for op in sorted(plan.operations, key=lambda o: o.operation_id):
        gd = guard_decisions.get(op.operation_id)
        guard_summary: dict[str, object]
        if gd is not None:
            guard_summary = {
                "approved": gd.approved,
                "blocked_reasons": sorted(gd.blocked_reasons),
                "proposal_id": gd.proposal_id,
                "action": gd.action.value,
            }
        else:
            guard_summary = {"missing": True}

        ops_canonical.append(
            {
                "operation_id": op.operation_id,
                "decision_id": op.decision_id,
                "queue_item_id": op.queue_item_id,
                "candidate_id": op.candidate_id,
                "action": op.action.value,
                "guard_decision_approved": op.guard_decision_approved,
                "guard_blocked_reasons": sorted(op.guard_blocked_reasons),
                "is_blocked": op.is_blocked,
                "requires_human_confirmation": op.requires_human_confirmation,
                "source_apply_result_ref": op.source_apply_result_ref,
                "guard_decision": guard_summary,
            }
        )

    canonical = json.dumps(
        {
            "plan_id": plan.plan_id,
            "idempotency_key": idempotency_key,
            "operations": ops_canonical,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_block_reason(results: list[GuardedOperationResult]) -> str:
    """Build a concise plan-level block reason from per-operation results."""
    blocked = [r for r in results if r.execution_status == ApplyExecutionStatus.BLOCKED]
    if not blocked:
        return ""
    reasons = [f"{r.operation_id}: {r.reason}" for r in blocked[:3]]
    if len(blocked) > 3:
        reasons.append(f"... and {len(blocked) - 3} more")
    return "Blocked operations: " + "; ".join(reasons)


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


def execute_apply_plan_guarded(
    plan: ReconciliationApplyPlan,
    *,
    guard_decisions_by_operation_id: dict[str, FinalMutationGuardDecision],
    idempotency_key: str,
    clock: Callable[[], str] | None = None,
) -> GuardedApplyExecutionResult:
    """Convenience: execute an apply plan through a fresh guarded runtime.

    Each call creates a new ``GuardedApplyRuntime``, so there is no shared
    idempotency state across calls.  Re-running with the same
    ``idempotency_key`` will not detect a prior execution from a separate
    call of this function.

    For durable idempotency that survives process restarts, construct a
    ``GuardedApplyRuntime`` with a ``GuardedApplyExecutionRepository``.
    """
    runtime = GuardedApplyRuntime()
    return runtime.execute(
        plan,
        guard_decisions_by_operation_id=guard_decisions_by_operation_id,
        idempotency_key=idempotency_key,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Public fingerprint builder
# ---------------------------------------------------------------------------


def build_guarded_apply_execution_fingerprint(
    plan: ReconciliationApplyPlan,
    guard_decisions_by_operation_id: dict[str, FinalMutationGuardDecision],
    idempotency_key: str,
) -> str:
    """Public accessor for the deterministic execution fingerprint.

    This is the same fingerprint used internally by
    ``GuardedApplyRuntime`` for idempotency. It is exposed as a stable
    helper so tests and persistence adapters can compute the same
    fingerprint without duplicating logic.
    """
    return _build_execution_fingerprint(plan, guard_decisions_by_operation_id, idempotency_key)


__all__ = [
    "GuardedApplyRuntime",
    "GuardedApplyRuntimeError",
    "build_guarded_apply_execution_fingerprint",
    "execute_apply_plan_guarded",
]
