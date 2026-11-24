"""Resolve the complete effective linked refund set at one ledger prefix."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from ..contracts import LifecycleError, canonical, digest
from .accounts import bundle_ref
from .contracts import BundleFactRefV1, strict_json
from .movement_contracts import MOVEMENT_FORMAT
from .refund_allowance_v1 import (
    ActiveRefundAllocationV1,
    RefundExpenseV1,
    _expense_from_fact,
    compile_refund_allowance_v1,
)
from .refund_facts_v1 import REFUND_FORMAT, project_refund_fact_v1


def _linked_refund_rows(
    conn, record_id, economic_event_id, before_seq, *, include_void=False, include_prior=False
):
    return conn.execute(
        """
        WITH latest AS (
            SELECT record_id,max(version) AS version FROM l2_facts
            WHERE (:cutoff IS NULL OR commit_seq<:cutoff)
            GROUP BY record_id
        )
        SELECT f.* FROM l2_facts AS f JOIN latest USING (record_id,version)
        WHERE json_extract(f.values_json,'$.format')=:format
          AND (:include_void OR json_extract(f.values_json,'$.status') IS NOT 'VOID')
          AND (
              json_extract(f.values_json,'$.expense.economic_event_id')=:event_id
              OR json_extract(f.values_json,'$.expense.fact_ref.record_id')=:record_id
              OR (:include_prior AND EXISTS (
                  SELECT 1 FROM l2_facts AS old
                  WHERE old.record_id=f.record_id
                    AND (:cutoff IS NULL OR old.commit_seq<:cutoff)
                    AND json_extract(old.values_json,'$.format')=:format
                    AND (
                        json_extract(old.values_json,'$.expense.economic_event_id')=:event_id
                        OR json_extract(old.values_json,'$.expense.fact_ref.record_id')=:record_id
                    )
              ))
          )
        ORDER BY f.record_id
        """,
        {
            "cutoff": before_seq,
            "include_void": include_void,
            "include_prior": include_prior,
            "format": REFUND_FORMAT,
            "event_id": economic_event_id,
            "record_id": record_id,
        },
    )


def _allocations_from_rows(conn, expense, rows) -> tuple[ActiveRefundAllocationV1, ...]:
    target = expense.current_fact
    active = []
    checked = {target}
    for row in rows:
        projected = project_refund_fact_v1(strict_json(row["values_json"]))
        if projected.credit_effect is None:
            continue
        observed = projected.observed_expense
        if observed not in checked:
            original = conn.execute(
                "SELECT * FROM l2_facts WHERE record_id=? AND version=?",
                (observed.record_id, observed.version),
            ).fetchone()
            if original is None or bundle_ref(original) != observed:
                raise LifecycleError("Refund allocation lacks its exact observed expense fact")
            original_expense = _expense_from_fact(original)
            if (
                original_expense.economic_event_id != expense.economic_event_id
                or original_expense.original_currency != expense.original_currency
            ):
                raise LifecycleError(
                    "Observed expense differs from the current refund relationship"
                )
            checked.add(observed)
        active.append(
            ActiveRefundAllocationV1(
                bundle_ref(row),
                observed,
                projected.expense_economic_event_id,
                projected.terms.original_currency,
                projected.terms.original_amount,
            )
        )
    return tuple(active)


def _refund_state_rows(conn, expense: RefundExpenseV1, before_seq: int | None = None):
    if not conn.in_transaction:
        raise LifecycleError("Refund inventory requires one protected ledger transaction")
    target = expense.current_fact
    if before_seq is not None and (type(before_seq) is not int or target.commit_seq >= before_seq):
        raise LifecycleError("Refund expense is outside the requested ledger prefix")
    return tuple(
        _linked_refund_rows(
            conn, target.record_id, expense.economic_event_id, before_seq, include_void=True
        )
    )


def refund_inventory_snapshot_v1(
    conn, expense: RefundExpenseV1, before_seq: int | None = None
) -> tuple[tuple[ActiveRefundAllocationV1, ...], str]:
    """Include reversed records in the identity so an old card cannot revive."""
    rows = _refund_state_rows(conn, expense, before_seq)
    active = _allocations_from_rows(conn, expense, rows)
    lineage_rows = _linked_refund_rows(
        conn,
        expense.current_fact.record_id,
        expense.economic_event_id,
        before_seq,
        include_void=True,
        include_prior=True,
    )
    lineage_sha256 = digest([asdict(bundle_ref(row)) for row in lineage_rows])
    return active, lineage_sha256


def active_refund_allocations_v1(
    conn, expense: RefundExpenseV1, before_seq: int | None = None
) -> tuple[ActiveRefundAllocationV1, ...]:
    """Read within the caller's protected transaction, including historical replay cuts."""
    return _allocations_from_rows(conn, expense, _refund_state_rows(conn, expense, before_seq))


class LinkedRefundConflictV1(LifecycleError):
    """A retained expense preparation needs one or more linked refunds resolved."""

    def __init__(
        self,
        message: str,
        original: BundleFactRefV1,
        allocations: tuple[ActiveRefundAllocationV1, ...],
    ):
        super().__init__(message)
        self.original = original
        self.refund_refs = tuple(item.fact for item in allocations)


def require_refund_compatible_expense_change(
    conn, previous, values: dict[str, Any], *, before_seq: int | None = None
) -> None:
    """Reject changes that would invalidate an existing, effective linked refund."""
    if previous is None:
        return
    predecessor = strict_json(previous["values_json"])
    if predecessor.get("format") not in {None, MOVEMENT_FORMAT}:
        return
    rows = _linked_refund_rows(
        conn, previous["record_id"], previous["economic_event_id"], before_seq
    )
    active_rows = tuple(rows)
    if not active_rows:
        return
    if before_seq is None and not conn.in_transaction:
        raise LifecycleError("Refund expense change requires a protected ledger transaction")
    expense = _expense_from_fact(previous)
    allocations = _allocations_from_rows(conn, expense, active_rows)
    if values.get("format") == MOVEMENT_FORMAT:
        if values["kind"] != "expense" or values["status"] != "ACTIVE":
            raise LinkedRefundConflictV1(
                "An expense with active linked refunds cannot be reversed",
                expense.current_fact,
                allocations,
            )
        amount, currency = values["original_amount"], values["original_currency"]
    elif "format" not in values:
        from ..effective_v1 import _business_fields

        fields = _business_fields(canonical(values))
        amount, currency = fields["amount"], fields["currency"]
    else:
        raise LinkedRefundConflictV1(
            "Expense correction cannot invalidate active linked refunds",
            expense.current_fact,
            allocations,
        )
    if currency != expense.original_currency:
        raise LinkedRefundConflictV1(
            "Active linked refunds require the original expense currency",
            expense.current_fact,
            allocations,
        )
    allowance = compile_refund_allowance_v1(expense, allocations)
    if Decimal(amount) < Decimal(allowance.refunded_original):
        raise LinkedRefundConflictV1(
            "Expense correction would make active refunds exceed the original currency allowance",
            expense.current_fact,
            allowance.active_refunds,
        )
