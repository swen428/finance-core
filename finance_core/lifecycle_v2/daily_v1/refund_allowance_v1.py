"""Compute the original-currency allowance from one verified refund inventory."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Literal

from finance_core.money import SUPPORTED_CURRENCIES, MoneyValidationError, canonical_money_str

from ..contracts import LifecycleError, digest
from .accounts import MAX_RECORDS, DailyAccountServiceV1, _audit, bundle_ref
from .contracts import (
    BUNDLE_FORMATS,
    AccountDefinitionRefV1,
    BundleFactRefV1,
    OpeningRefV1,
    _money,
    account_ref,
    opening_ref,
    strict_json,
    text,
)
from .movement_contracts import MOVEMENT_FORMAT, _day, _later


def _positive(amount: str, currency: str) -> None:
    if type(currency) is not str or currency not in SUPPORTED_CURRENCIES:
        raise LifecycleError("Exact original refund currency required")
    try:
        canonical = _money(amount, currency)
    except MoneyValidationError:
        raise LifecycleError("Refund amount exceeds its original currency precision") from None
    if canonical != amount or Decimal(canonical) <= 0:
        raise LifecycleError("Canonical positive original refund amount required")


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


def read_refundable_expense_v1(
    service, selected: BundleFactRefV1, *, terms: RefundTermsV1 | None = None
) -> RefundExpenseV1:
    """Qualify the current expense and any proposed credit in the same protected read."""
    if type(selected) is not BundleFactRefV1:
        raise LifecycleError("Exact selected expense fact required")
    if terms is not None and type(terms) is not RefundTermsV1:
        raise LifecycleError("Exact refund terms required")
    with DailyAccountServiceV1(service)._read() as (bound, conn):
        _audit(bound, conn)
        expense = _current_refundable_expense(conn, selected)
        if terms is not None:
            _current_credit_account(conn, expense, terms)
        return expense


def _current_refundable_expense(conn, selected: BundleFactRefV1) -> RefundExpenseV1:
    row = conn.execute(
        "SELECT * FROM l2_facts WHERE record_id=? ORDER BY version DESC LIMIT 1",
        (selected.record_id,),
    ).fetchone()
    if row is None or bundle_ref(row) != selected:
        raise LifecycleError("Refund target is no longer the selected expense version")
    values = strict_json(row["values_json"])
    if (
        values.get("format") == MOVEMENT_FORMAT
        and values["kind"] == "expense"
        and values["status"] == "ACTIVE"
    ):
        amount, currency = values["original_amount"], values["original_currency"]
    elif "format" not in values:
        from ..effective_v1 import _business_fields

        original = _business_fields(row["values_json"])
        amount, currency = original["amount"], original["currency"]
    else:
        raise LifecycleError("Linked refund requires a current active expense")
    return RefundExpenseV1(selected, row["economic_event_id"], currency, amount)


def _current_credit_account(conn, expense: RefundExpenseV1, terms: RefundTermsV1):
    if terms.original_currency != expense.original_currency:
        raise LifecycleError("Refund must use the expense's original currency")
    row = conn.execute(
        "SELECT * FROM l2_facts WHERE record_id=? ORDER BY version DESC LIMIT 1",
        (terms.credit_account.account_id,),
    ).fetchone()
    values = strict_json(row["values_json"]) if row is not None else {}
    if (
        row is None
        or values.get("format") not in BUNDLE_FORMATS
        or account_ref(values) != terms.credit_account
        or values["account"]["currency"] != terms.credit_currency
    ):
        raise LifecycleError("Refund credit requires the current account and currency")
    return bundle_ref(row), values


@dataclass(frozen=True)
class RefundTermsV1:
    original_currency: str
    original_amount: str
    credit_account: AccountDefinitionRefV1
    credit_currency: str
    credit_amount: str

    def __post_init__(self) -> None:
        if type(self.credit_account) is not AccountDefinitionRefV1:
            raise LifecycleError("Exact refund receipt account required")
        _positive(self.original_amount, self.original_currency)
        _positive(self.credit_amount, self.credit_currency)


@dataclass(frozen=True)
class RefundCreditPreviewV1:
    """Current dependencies and opening information for a proposed refund credit."""

    expense: RefundExpenseV1
    terms: RefundTermsV1
    received_date: str
    credit_account_fact: BundleFactRefV1
    active_opening: OpeningRefV1 | None
    opening_requirement: Literal[
        "no_active_opening", "strictly_later", "authenticated_reply_required"
    ]


def _refund_funds_time(received_date: str) -> dict[str, str | None]:
    return {
        "format": "finance-daily-as-of-v1",
        "precision": "date",
        "timestamp": None,
        "local_date": received_date,
        "timezone": "Asia/Singapore",
    }


def read_refund_credit_preview_v1(
    service, selected: BundleFactRefV1, terms: RefundTermsV1, received_date: str
) -> RefundCreditPreviewV1:
    """Read whether the actual receipt date needs an explicit opening balance reply."""
    if type(selected) is not BundleFactRefV1 or type(terms) is not RefundTermsV1:
        raise LifecycleError("Exact selected expense fact and refund terms required")
    received = _day(received_date)
    funds_time = _refund_funds_time(received)
    with DailyAccountServiceV1(service)._read() as (bound, conn):
        _audit(bound, conn)
        expense = _current_refundable_expense(conn, selected)
        credit_fact, credit = _current_credit_account(conn, expense, terms)
        opening = credit["opening"]
        if opening is None or opening["status"] != "ACTIVE":
            return RefundCreditPreviewV1(
                expense, terms, received, credit_fact, None, "no_active_opening"
            )
        return RefundCreditPreviewV1(
            expense,
            terms,
            received,
            credit_fact,
            opening_ref(credit),
            "strictly_later"
            if _later(funds_time, opening["as_of"])
            else "authenticated_reply_required",
        )


@dataclass(frozen=True)
class RefundAmountPreviewV1:
    allowance: RefundAllowanceV1
    terms: RefundTermsV1
    replaced: BundleFactRefV1 | None
    projected_remaining_original: str


def preview_refund_amount_v1(
    expense: RefundExpenseV1,
    active_refunds: tuple[ActiveRefundAllocationV1, ...],
    terms: RefundTermsV1,
    *,
    replaced: BundleFactRefV1 | None = None,
) -> RefundAmountPreviewV1:
    """Preview only the original limit and separate account credit for a complete card."""
    allowance = compile_refund_allowance_v1(expense, active_refunds)
    if type(terms) is not RefundTermsV1 or terms.original_currency != expense.original_currency:
        raise LifecycleError("Refund must use the expense's original currency for its allowance")
    if replaced is not None and type(replaced) is not BundleFactRefV1:
        raise LifecycleError("Exact current refund fact required for replacement")
    replaced_amount = next(
        (item.original_amount for item in allowance.active_refunds if item.fact == replaced),
        None,
    )
    if replaced is not None and replaced_amount is None:
        raise LifecycleError("Refund to replace is no longer current in this expense relationship")
    with localcontext(Context(prec=256, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)):
        available = Decimal(allowance.remaining_original) + Decimal(replaced_amount or "0")
        remaining = available - Decimal(terms.original_amount)
        if remaining < 0:
            raise LifecycleError("Refunds exceed the expense's original currency allowance")
        return RefundAmountPreviewV1(
            allowance,
            terms,
            replaced,
            canonical_money_str(remaining, expense.original_currency),
        )
