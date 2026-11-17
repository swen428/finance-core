"""Frozen currency admission for existing parser and proposal contracts.

Money's current registry can grow without changing historical parser results,
proposal hashes, completion decisions or independent-AI evidence reconstruction.
The original eight-code policy and its validation errors remain versioned here;
no policy field is added to existing DTOs or persisted payloads.
"""

from __future__ import annotations

from decimal import Decimal

from finance_core.money import MoneyValidationError, validate_amount_for_currency

SUPPORTED_CURRENCIES_V1: frozenset[str] = frozenset(
    {"SGD", "USD", "EUR", "GBP", "AUD", "CNY", "HKD", "JPY"}
)


def normalize_currency_v1(raw: str) -> str:
    """Apply the original Money currency policy, including original errors."""
    if not isinstance(raw, str):
        raise MoneyValidationError(f"Currency must be a string, got {type(raw).__name__}")
    normalized = raw.strip().upper()
    if not normalized:
        raise MoneyValidationError("Currency code must not be empty")
    if len(normalized) != 3:
        raise MoneyValidationError(
            f"Currency code must be 3 characters, got {normalized!r} ({len(normalized)} chars)"
        )
    if not normalized.isalpha():
        raise MoneyValidationError(f"Currency code must contain only letters, got {normalized!r}")
    if normalized not in SUPPORTED_CURRENCIES_V1:
        raise MoneyValidationError(
            f"Unsupported currency {normalized!r}. "
            f"Supported currencies: {sorted(SUPPORTED_CURRENCIES_V1)}"
        )
    return normalized


def validate_amount_for_currency_v1(
    amount: Decimal, currency: str, *, label: str = "monetary value"
) -> Decimal:
    """Keep receipt role helpers on the same original currency admission."""
    return validate_amount_for_currency(amount, normalize_currency_v1(currency), label=label)
