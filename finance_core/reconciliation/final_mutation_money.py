"""Canonical Money Contract boundary for reconciliation CREATE mutations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from finance_core.money import (
    MoneyValidationError,
    SignPolicy,
    canonical_money_str,
    money_decimal,
    normalize_currency,
    validate_amount_for_currency,
)


class CreateMoneyValidationIssue(str, Enum):
    """Stable issue categories for invalid reconciliation CREATE money."""

    AMOUNT = "amount"
    CURRENCY = "currency"


class CreateMoneyValidationError(MoneyValidationError):
    """One or both CREATE monetary fields failed the shared Money Contract."""

    def __init__(self, issues: tuple[CreateMoneyValidationIssue, ...]) -> None:
        self.issues = issues
        joined = ", ".join(issue.value for issue in issues)
        super().__init__(f"Invalid reconciliation CREATE money field(s): {joined}")


@dataclass(frozen=True)
class ValidatedReconciliationCreateMoney:
    """Immutable authoritative CREATE money reused across the final-write path."""

    amount: Decimal
    currency: str
    canonical_amount: str


def validate_reconciliation_create_money(
    amount: object,
    currency: object,
) -> ValidatedReconciliationCreateMoney:
    """Validate and canonicalize reconciliation CREATE money exactly once.

    Amount and currency are checked independently so callers can emit stable,
    deterministic reason codes for both fields. Arbitrary runtime values are
    never serialized here. The original amount is passed to ``money_decimal``;
    no float coercion, quantization, rounding, or FX conversion is performed.
    """
    issues: list[CreateMoneyValidationIssue] = []
    normalized_currency: str | None = None
    decimal_amount: Decimal | None = None

    if not isinstance(currency, str):
        issues.append(CreateMoneyValidationIssue.CURRENCY)
    else:
        try:
            normalized_currency = normalize_currency(currency)
        except MoneyValidationError:
            issues.append(CreateMoneyValidationIssue.CURRENCY)

    if isinstance(amount, bool) or not isinstance(amount, (Decimal, int, str)):
        issues.append(CreateMoneyValidationIssue.AMOUNT)
    else:
        try:
            decimal_amount = money_decimal(amount, label="reconciliation CREATE amount")
        except MoneyValidationError:
            issues.append(CreateMoneyValidationIssue.AMOUNT)

    if normalized_currency is None and decimal_amount is not None:
        try:
            SignPolicy.STRICTLY_POSITIVE.enforce(  # type: ignore[attr-defined]
                decimal_amount,
                label="reconciliation CREATE amount",
            )
        except MoneyValidationError:
            issues.append(CreateMoneyValidationIssue.AMOUNT)

    if normalized_currency is not None and decimal_amount is not None:
        try:
            validate_amount_for_currency(
                decimal_amount,
                normalized_currency,
                label="reconciliation CREATE amount",
            )
            SignPolicy.STRICTLY_POSITIVE.enforce(  # type: ignore[attr-defined]
                decimal_amount,
                label="reconciliation CREATE amount",
            )
            canonical_amount = canonical_money_str(decimal_amount, normalized_currency)
        except MoneyValidationError:
            if CreateMoneyValidationIssue.AMOUNT not in issues:
                issues.append(CreateMoneyValidationIssue.AMOUNT)
        else:
            if not issues:
                return ValidatedReconciliationCreateMoney(
                    amount=decimal_amount,
                    currency=normalized_currency,
                    canonical_amount=canonical_amount,
                )

    if CreateMoneyValidationIssue.AMOUNT in issues:
        ordered_issues: tuple[CreateMoneyValidationIssue, ...] = (
            CreateMoneyValidationIssue.AMOUNT,
        )
        if CreateMoneyValidationIssue.CURRENCY in issues:
            ordered_issues += (CreateMoneyValidationIssue.CURRENCY,)
    else:
        ordered_issues = (CreateMoneyValidationIssue.CURRENCY,)
    raise CreateMoneyValidationError(ordered_issues)


__all__ = [
    "CreateMoneyValidationError",
    "CreateMoneyValidationIssue",
    "ValidatedReconciliationCreateMoney",
    "validate_reconciliation_create_money",
]
