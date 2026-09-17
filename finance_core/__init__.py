"""Stable public API for the installable Finance core package.

The existing ``src`` modules remain internal during the staged package
migration. Consumers should import only from ``finance_core`` so internal
modules can move without changing the public contract.
"""

from finance_core.money import (
    SUPPORTED_CURRENCIES,
    CurrencyMismatchError,
    Money,
    MoneyValidationError,
    SignPolicy,
    canonical_decimal_str,
    canonical_money_str,
    minor_units,
    money_decimal,
    normalize_currency,
    quantize_for_currency,
    quantum_for_currency,
    require_same_currency,
    validate_amount_for_currency,
)

API_CONTRACT_VERSION = "finance-core-api-v1"

__all__ = [
    "API_CONTRACT_VERSION",
    "SUPPORTED_CURRENCIES",
    "CurrencyMismatchError",
    "Money",
    "MoneyValidationError",
    "SignPolicy",
    "canonical_decimal_str",
    "canonical_money_str",
    "minor_units",
    "money_decimal",
    "normalize_currency",
    "quantize_for_currency",
    "quantum_for_currency",
    "require_same_currency",
    "validate_amount_for_currency",
]
