"""Compute the original-currency allowance from one verified refund inventory."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from finance_core.money import SUPPORTED_CURRENCIES, MoneyValidationError, canonical_money_str

from ..contracts import LifecycleError, digest
from .accounts import MAX_RECORDS
from .contracts import BundleFactRefV1, _money, text


def _positive(amount: str, currency: str) -> str:
    if type(currency) is not str or currency not in SUPPORTED_CURRENCIES:
        raise LifecycleError("Exact original refund currency required")
    try:
        canonical = _money(amount, currency)
    except MoneyValidationError:
        raise LifecycleError("Refund amount exceeds its original currency precision") from None
    if canonical != amount or Decimal(canonical) <= 0:
        raise LifecycleError("Canonical positive original refund amount required")
    return canonical


@dataclass(frozen=True)
class RefundExpenseV1:
    current_fact: BundleFactRefV1
    economic_event_id: str
    original_currency: str
    original_amount: str

    def __post_init__(self) -> None:
        if type(self.current_fact) is not BundleFactRefV1:
            raise LifecycleError("Exact refundable expense fact required")
        text(self.economic_event_id, "refundable expense economic event")
        _positive(self.original_amount, self.original_currency)


@dataclass(frozen=True)
class ActiveRefundAllocationV1:
    fact: BundleFactRefV1
    observed_expense: BundleFactRefV1
    expense_economic_event_id: str
    original_currency: str
    original_amount: str

    def __post_init__(self) -> None:
        if (
            type(self.fact) is not BundleFactRefV1
            or type(self.observed_expense) is not BundleFactRefV1
            or self.fact.record_id == self.observed_expense.record_id
            or self.fact.commit_seq <= self.observed_expense.commit_seq
        ):
            raise LifecycleError("Refund allocation requires its own fact and original expense")
        text(self.expense_economic_event_id, "refund target economic event")
        _positive(self.original_amount, self.original_currency)


@dataclass(frozen=True)
class RefundAllowanceV1:
    expense: RefundExpenseV1
    active_refunds: tuple[ActiveRefundAllocationV1, ...]
    refunded_original: str
    remaining_original: str
    format: str = "finance-daily-refund-allowance-v1"

    @property
    def allowance_sha256(self) -> str:
        return digest(asdict(self))


def compile_refund_allowance_v1(
    expense: RefundExpenseV1,
    active_refunds: tuple[ActiveRefundAllocationV1, ...],
) -> RefundAllowanceV1:
    """Calculate against the full current inventory supplied by a protected ledger read."""
    if (
        type(expense) is not RefundExpenseV1
        or type(active_refunds) is not tuple
        or len(active_refunds) > MAX_RECORDS
        or any(type(entry) is not ActiveRefundAllocationV1 for entry in active_refunds)
    ):
        raise LifecycleError("Bounded exact refund inventory required")
    original = expense.current_fact
    ordered = tuple(sorted(active_refunds, key=lambda entry: entry.fact.record_id))
    if len({entry.fact.record_id for entry in ordered}) != len(ordered):
        raise LifecycleError("Refund allocation requires one current version per refund")
    for entry in ordered:
        observed = entry.observed_expense
        if (
            entry.expense_economic_event_id != expense.economic_event_id
            or entry.original_currency != expense.original_currency
            or observed.record_id != original.record_id
            or observed.version > original.version
            or (observed.version == original.version and observed != original)
            or (observed.version < original.version and observed.commit_seq >= original.commit_seq)
        ):
            raise LifecycleError("Refund allocation differs from the current expense relationship")
    with localcontext(Context(prec=256, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)):
        allocated = sum((Decimal(entry.original_amount) for entry in ordered), Decimal(0))
        remaining = Decimal(expense.original_amount) - allocated
        if remaining < 0:
            raise LifecycleError("Refunds exceed the expense's original currency allowance")
        return RefundAllowanceV1(
            expense,
            ordered,
            canonical_money_str(allocated, expense.original_currency),
            canonical_money_str(remaining, expense.original_currency),
        )
