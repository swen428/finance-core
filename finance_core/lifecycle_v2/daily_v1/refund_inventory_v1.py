"""Resolve the complete effective linked refund set at one ledger prefix."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

from ..contracts import LifecycleError, canonical, digest
from .accounts import bundle_ref
from .contracts import strict_json
from .movement_contracts import MOVEMENT_FORMAT
from .refund_allowance_v1 import (
    ActiveRefundAllocationV1,
    RefundExpenseV1,
    _expense_from_fact,
    compile_refund_allowance_v1,
)
from .refund_facts_v1 import REFUND_FORMAT, project_refund_fact_v1


def _linked_refund_rows(conn, record_id, economic_event_id, before_seq, *, include_void=False):
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
          )
        ORDER BY f.record_id
        """,
        {
            "cutoff": before_seq,
            "include_void": include_void,
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
    lineage_sha256 = digest([asdict(bundle_ref(row)) for row in rows])
    return active, lineage_sha256


def active_refund_allocations_v1(
    conn, expense: RefundExpenseV1, before_seq: int | None = None
) -> tuple[ActiveRefundAllocationV1, ...]:
    """Read within the caller's protected transaction, including historical replay cuts."""
    return _allocations_from_rows(conn, expense, _refund_state_rows(conn, expense, before_seq))


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
            raise LifecycleError("An expense with active linked refunds cannot be reversed")
        amount, currency = values["original_amount"], values["original_currency"]
    elif "format" not in values:
        from ..effective_v1 import _business_fields

        fields = _business_fields(canonical(values))
        amount, currency = fields["amount"], fields["currency"]
    else:
        raise LifecycleError("Expense correction cannot invalidate active linked refunds")
    if currency != expense.original_currency:
        raise LifecycleError("Active linked refunds require the original expense currency")
    compile_refund_allowance_v1(replace(expense, original_amount=amount), allocations)
