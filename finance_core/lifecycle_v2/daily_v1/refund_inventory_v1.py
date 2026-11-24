"""Resolve the complete effective linked refund set at one ledger prefix."""

from __future__ import annotations

from ..contracts import LifecycleError
from .accounts import bundle_ref
from .contracts import strict_json
from .refund_allowance_v1 import ActiveRefundAllocationV1, RefundExpenseV1, _expense_from_fact
from .refund_facts_v1 import REFUND_FORMAT, project_refund_fact_v1


def active_refund_allocations_v1(
    conn, expense: RefundExpenseV1, before_seq: int | None = None
) -> tuple[ActiveRefundAllocationV1, ...]:
    """Read within the caller's protected transaction, including historical replay cuts."""
    if not conn.in_transaction:
        raise LifecycleError("Refund inventory requires one protected ledger transaction")
    target = expense.current_fact
    if before_seq is not None and (type(before_seq) is not int or target.commit_seq >= before_seq):
        raise LifecycleError("Refund expense is outside the requested ledger prefix")
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT record_id,max(version) AS version FROM l2_facts
            WHERE (:cutoff IS NULL OR commit_seq<:cutoff)
            GROUP BY record_id
        )
        SELECT f.* FROM l2_facts AS f JOIN latest USING (record_id,version)
        WHERE json_extract(f.values_json,'$.format')=:format
          AND (
              json_extract(f.values_json,'$.expense.economic_event_id')=:event_id
              OR json_extract(f.values_json,'$.expense.fact_ref.record_id')=:record_id
          )
        ORDER BY f.record_id
        """,
        {
            "cutoff": before_seq,
            "format": REFUND_FORMAT,
            "event_id": expense.economic_event_id,
            "record_id": target.record_id,
        },
    )
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
