"""Reconstruct the exact refund dependencies a review and its confirmation share."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..contracts import LifecycleError, digest
from .accounts import bundle_ref
from .contracts import BundleFactRefV1, strict_json
from .refund_allowance_v1 import (
    RefundAllowanceExceededV1,
    RefundAllowanceV1,
    _current_credit_account,
    _expense_from_fact,
    _fact_at,
    _refund_opening_requirement,
    compile_refund_allowance_v1,
    preview_refund_amount_v1,
)
from .refund_facts_v1 import project_refund_fact_v1, require_refund_predecessor_v1
from .refund_inventory_v1 import refund_inventory_snapshot_v1


@dataclass(frozen=True)
class PriorRefundAllocationV1:
    allowance_before: RefundAllowanceV1
    remaining_original_after: str
    lineage_sha256: str


@dataclass(frozen=True)
class RefundPostingStateV1:
    allowance_before: RefundAllowanceV1
    replaced: BundleFactRefV1 | None
    credit_account_fact: BundleFactRefV1 | None
    remaining_original_after: str | None
    refund_lineage_sha256: str
    available_if_pending: str | None = None
    pending_opening_sha256: str | None = None
    previous_allocation: PriorRefundAllocationV1 | None = None
    format: str = "finance-daily-refund-posting-state-v1"

    @property
    def state_sha256(self) -> str:
        return digest(asdict(self))


def _previous_allocation(conn, previous, replaced, before_seq):
    row = _fact_at(conn, previous.observed_expense.record_id, before_seq)
    if row is None:
        raise LifecycleError("Original refund allocation expense is missing")
    expense = _expense_from_fact(row)
    if expense.economic_event_id != previous.expense_economic_event_id:
        raise LifecycleError("Original refund allocation expense identity differs")
    current, lineage = refund_inventory_snapshot_v1(conn, expense, before_seq)
    if not any(item.fact == replaced for item in current):
        raise LifecycleError("Refund is absent from its original allocation")
    allowance = compile_refund_allowance_v1(expense, current)
    after = compile_refund_allowance_v1(
        expense, tuple(item for item in current if item.fact != replaced)
    )
    return PriorRefundAllocationV1(allowance, after.remaining_original, lineage)


def refund_posting_state_v1(
    conn,
    values: dict[str, Any],
    replaced: BundleFactRefV1 | None = None,
    *,
    before_seq: int | None = None,
    allow_pending: bool = False,
) -> RefundPostingStateV1:
    """Compute financial eligibility at this cut inside the caller's protected transaction.

    The caller authenticates the actual evidence and compares the entire signed
    review before finalization. That same writer must append before releasing the lock.
    """
    if not conn.in_transaction:
        raise LifecycleError("Refund posting dependencies require one ledger transaction")
    if before_seq is not None and (type(before_seq) is not int or before_seq <= 0):
        raise LifecycleError("Exact refund history prefix required")

    projection = project_refund_fact_v1(values, allow_pending=allow_pending)
    previous_values = None
    if replaced is not None:
        if type(replaced) is not BundleFactRefV1:
            raise LifecycleError("Exact refund predecessor required")
        previous = _fact_at(conn, replaced.record_id, before_seq)
        if previous is None or bundle_ref(previous) != replaced:
            raise LifecycleError("Refund predecessor is no longer current")
        previous_values = strict_json(previous["values_json"])
    require_refund_predecessor_v1(previous_values, values, allow_pending=allow_pending)

    row = _fact_at(conn, projection.observed_expense.record_id, before_seq)
    if row is None:
        raise LifecycleError("Refund original expense is absent")
    expense = _expense_from_fact(row)
    if expense.economic_event_id != projection.expense_economic_event_id:
        raise LifecycleError("Refund must retain the original expense economic event")
    active, lineage_sha256 = refund_inventory_snapshot_v1(conn, expense, before_seq)
    previous_allocation = None
    replacement_in_target = replaced
    if previous_values is not None:
        old = project_refund_fact_v1(previous_values)
        if old.observed_expense.record_id != expense.current_fact.record_id:
            previous_allocation = _previous_allocation(conn, old, replaced, before_seq)
            replacement_in_target = None
    effect = projection.credit_effect
    if effect is None:
        if not any(entry.fact == replaced for entry in active):
            raise LifecycleError("Refund to reverse is no longer in the current allowance")
        allowance = compile_refund_allowance_v1(expense, active)
        remaining = compile_refund_allowance_v1(
            expense, tuple(entry for entry in active if entry.fact != replaced)
        ).remaining_original
        return RefundPostingStateV1(allowance, replaced, None, remaining, lineage_sha256)

    if expense.current_fact != projection.observed_expense:
        raise LifecycleError("Refund original expense version changed")
    account_fact, account = _current_credit_account(conn, expense, projection.terms, before_seq)
    opening, requirement = _refund_opening_requirement(account, effect["funds_time"])
    if effect["opening_ref"] != (None if opening is None else asdict(opening)):
        raise LifecycleError("Refund credit opening reference changed")
    basis = "authenticated_reply" if requirement == "authenticated_reply_required" else requirement
    coverage = effect["coverage"]
    pending_opening = (
        allow_pending and basis == "authenticated_reply" and coverage["basis"] == "awaiting_reply"
    )
    if coverage["basis"] != basis and not pending_opening:
        raise LifecycleError("Refund credit coverage differs from its actual opening time")
    pending_sha256 = coverage["binding_sha256"] if pending_opening else None
    try:
        amount = preview_refund_amount_v1(
            expense, active, projection.terms, replaced=replacement_in_target
        )
    except RefundAllowanceExceededV1 as conflict:
        if not allow_pending:
            raise
        return RefundPostingStateV1(
            conflict.allowance,
            replaced,
            account_fact,
            None,
            lineage_sha256,
            available_if_pending=conflict.available,
            pending_opening_sha256=pending_sha256,
            previous_allocation=previous_allocation,
        )
    return RefundPostingStateV1(
        amount.allowance,
        replaced,
        account_fact,
        amount.projected_remaining_original,
        lineage_sha256,
        pending_opening_sha256=pending_sha256,
        previous_allocation=previous_allocation,
    )
