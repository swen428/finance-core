"""Unified Money Contract — authoritative monetary representation and currency rules.

This module is the single shared authority for:

* currency normalization and validation
* currency minor-unit lookup
* authoritative Decimal conversion
* monetary amount validation (finite, precision, scale)
* same-currency assertions
* sign-policy enforcement
* canonical monetary serialization

All monetary boundaries in the receipt-split, finalization, settlement,
and audit paths must use this contract.  No implicit FX conversion is
performed — cross-currency arithmetic is rejected until an explicit
future FX contract exists.

Usage::

    from finance_core import (
        Money,
        normalize_currency,
        minor_units,
        money_decimal,
        validate_amount_for_currency,
        require_same_currency,
        SignPolicy,
        canonical_decimal_str,
    )
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MONEY_QUANTUM = Decimal("0.01")
ZERO = Decimal("0.00")

# Matches scientific-notation patterns like 1.5e2, 1E+4, 2.3e-5.
# The 'e' must be immediately between a digit and a digit/sign.
_SCIENTIFIC_RE = re.compile(r"\d[eE][+-]?\d")


# ---------------------------------------------------------------------------
# Supported currency registry
# ---------------------------------------------------------------------------

# Currency -> minor-unit decimal places.
# Every currency used by the repository must have an explicit rule.
# Do NOT claim full ISO 4217 support — this registry is intentionally
# scoped to the application's current needs.
_SUPPORTED_CURRENCY_MINOR_UNITS: dict[str, int] = {
    "SGD": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "AUD": 2,
    "CNY": 2,
    "HKD": 2,
    "JPY": 0,
}

SUPPORTED_CURRENCIES: frozenset[str] = frozenset(_SUPPORTED_CURRENCY_MINOR_UNITS.keys())


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MoneyValidationError(ValueError):
    """A monetary value or currency failed the Money Contract."""


class CurrencyMismatchError(MoneyValidationError):
    """Two monetary values have different currencies and cannot be combined."""


# ---------------------------------------------------------------------------
# Currency normalization and validation
# ---------------------------------------------------------------------------


def normalize_currency(raw: str) -> str:
    """Normalize and validate a currency code.

    Accepts case-insensitive input, returns uppercase ISO-style 3-letter code.
    Rejects malformed, empty, or unsupported currency codes.
    """
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
    if normalized not in SUPPORTED_CURRENCIES:
        raise MoneyValidationError(
            f"Unsupported currency {normalized!r}. "
            f"Supported currencies: {sorted(SUPPORTED_CURRENCIES)}"
        )
    return normalized


def minor_units(currency: str) -> int:
    """Return the number of minor-unit decimal places for *currency*.

    The currency is normalized and validated first, so this accepts
    case-insensitive input and rejects unsupported codes.
    """
    normalized = normalize_currency(currency)
    return _SUPPORTED_CURRENCY_MINOR_UNITS[normalized]


# ---------------------------------------------------------------------------
# Authoritative Decimal conversion
# ---------------------------------------------------------------------------


def money_decimal(value: object, *, label: str = "monetary value") -> Decimal:
    """Convert *value* to a Decimal, rejecting all unsafe input types.

    Accepted inputs:
    - ``Decimal``
    - ``int``
    - canonical decimal string (e.g. ``"12.30"``)

    Rejected inputs:
    - ``float`` (including ``0.1``)
    - ``bool``
    - ``None``
    - empty string
    - malformed strings
    - scientific notation
    - ``NaN``, ``Infinity``, ``-Infinity`` (as float or string)

    This is the single authoritative entry point for monetary values.
    Every financial boundary must route through this function or the
    ``Money`` value object.
    """
    if isinstance(value, bool):
        raise MoneyValidationError(f"{label} must not be a boolean, got {value!r}")
    if isinstance(value, float):
        raise MoneyValidationError(
            f"{label} is float ({value!r}); monetary values must not be floats"
        )
    if value is None:
        raise MoneyValidationError(f"{label} is required, got None")

    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise MoneyValidationError(f"{label} must not be an empty string")
        # Reject scientific notation.
        if _looks_like_scientific(stripped):
            raise MoneyValidationError(
                f"{label} {value!r} appears to use scientific notation; "
                f"canonical decimal strings only"
            )
        try:
            dec = Decimal(stripped)
        except (InvalidOperation, ValueError) as exc:
            raise MoneyValidationError(f"{label} {value!r} is not a valid Decimal string") from exc
    else:
        raise MoneyValidationError(
            f"{label} must be Decimal, int, or canonical string; "
            f"got {type(value).__name__}: {value!r}"
        )

    # Reject non-finite values.
    if not dec.is_finite():
        raise MoneyValidationError(f"{label} must be a finite Decimal, got {dec!r}")

    return dec


def _looks_like_scientific(s: str) -> bool:
    """Return True if *s* looks like scientific notation.

    Matches patterns like 1.5e2, 1E+4, 2.3e-5, 1e10, etc.
    Does not match isolated 'e' in ordinary words like "not-a-number".
    """
    return bool(_SCIENTIFIC_RE.search(s))


# ---------------------------------------------------------------------------
# Amount scale / precision validation
# ---------------------------------------------------------------------------


def validate_amount_for_currency(
    amount: Decimal,
    currency: str,
    *,
    label: str = "monetary value",
) -> Decimal:
    """Validate that *amount* does not exceed the currency's minor-unit scale.

    Returns *amount* unchanged on success so callers can chain::

        amount = validate_amount_for_currency(money_decimal(raw), currency)

    For SGD (2 minor units), values like 12.345 are rejected because the
    sub-minor-unit precision cannot be silently rounded.

    This validation is about representation precision, not about whether
    the value is rounded to the quantum — ``quantize()`` is a separate
    deliberate operation owned by the calculator.
    """
    units = minor_units(currency)
    quantum = Decimal("0.1") ** units

    # Check whether amount has sub-minor-unit precision.
    if amount == amount.quantize(quantum):
        return amount

    raise MoneyValidationError(
        f"{label} {amount} has precision beyond {units} minor unit(s) "
        f"for {currency}; sub-minor-unit precision is not accepted"
    )


def quantize_for_currency(
    amount: Decimal,
    currency: str,
    *,
    rounding: str = ROUND_HALF_UP,
) -> Decimal:
    """Explicitly quantize *amount* to the currency's minor-unit scale.

    This is a deliberate rounding operation.  Callers must use it
    intentionally — it is never applied silently by validation.
    """
    units = minor_units(currency)
    quantum = _quantum_for_units(units)
    return amount.quantize(quantum, rounding=rounding)


def quantum_for_currency(currency: str) -> Decimal:
    """Return the quantum (smallest unit) for *currency*.

    For SGD (2 minor units) this is ``Decimal("0.01")``.
    For JPY (0 minor units) this is ``Decimal("1")``.
    """
    units = minor_units(currency)
    return _quantum_for_units(units)


def _quantum_for_units(units: int) -> Decimal:
    return Decimal("0.1") ** units


# ---------------------------------------------------------------------------
# Sign policy
# ---------------------------------------------------------------------------


class SignPolicy:
    """Sign-policy constants for monetary amounts.

    Domain-specific sign rules remain at the call site.  This contract
    validates representation and finiteness; callers declare their own
    sign constraints.

    Usage::

        amount = money_decimal(raw, label="item price")
        SignPolicy.NON_NEGATIVE.enforce(amount, label="item price")
    """

    _name: str
    _check: Any  # callable

    def __init__(self, name: str, check: Any) -> None:
        self._name = name
        self._check = check

    def enforce(self, amount: Decimal, *, label: str = "monetary value") -> Decimal:
        """Validate *amount* against this sign policy.

        Returns *amount* unchanged on success.
        """
        self._check(amount, label)
        return amount

    def __repr__(self) -> str:
        return f"SignPolicy.{self._name}"

    @staticmethod
    def _any(amount: Decimal, label: str) -> None:
        """Accept any finite amount (including negative, zero, positive)."""
        pass  # finiteness already validated by money_decimal()

    @staticmethod
    def _non_negative(amount: Decimal, label: str) -> None:
        """Reject negative amounts."""
        if amount < ZERO:
            raise MoneyValidationError(f"{label} must be non-negative, got {amount}")

    @staticmethod
    def _strictly_positive(amount: Decimal, label: str) -> None:
        """Reject zero and negative amounts."""
        if amount <= ZERO:
            raise MoneyValidationError(f"{label} must be strictly positive, got {amount}")


SignPolicy.ANY = SignPolicy("ANY", SignPolicy._any)  # type: ignore[attr-defined]
SignPolicy.NON_NEGATIVE = SignPolicy("NON_NEGATIVE", SignPolicy._non_negative)  # type: ignore[attr-defined]
SignPolicy.STRICTLY_POSITIVE = SignPolicy("STRICTLY_POSITIVE", SignPolicy._strictly_positive)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Same-currency assertion
# ---------------------------------------------------------------------------


def require_same_currency(
    currency_a: str,
    currency_b: str,
    *,
    label_a: str = "first currency",
    label_b: str = "second currency",
) -> None:
    """Assert that two currency codes refer to the same currency.

    Both codes are normalized before comparison so that case-insensitive
    input is accepted.

    Raises ``CurrencyMismatchError`` if they differ.
    """
    a = normalize_currency(currency_a)
    b = normalize_currency(currency_b)
    if a != b:
        raise CurrencyMismatchError(
            f"{label_a} {a!r} does not match {label_b} {b!r}; "
            f"cross-currency operations are not supported"
        )


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------


def canonical_decimal_str(amount: Decimal) -> str:
    """Serialize a Decimal as a normalised canonical string.

    Equivalent finite Decimal values always produce the same string
    regardless of how they were constructed::

        canonical_decimal_str(Decimal("12.300")) -> "12.3"
        canonical_decimal_str(Decimal("12.000")) -> "12"
        canonical_decimal_str(Decimal("0.00"))   -> "0"
        canonical_decimal_str(Decimal("-0.00"))  -> "0"

    This is the generic canonical Decimal representation — it strips
    trailing zeros and normalises negative zero.  It is appropriate for
    hashing and content-addressable identifiers.

    Scientific notation is never emitted; all output uses plain decimal
    notation.

    For monetary snapshots that must preserve currency-scale semantics
    (e.g. two-decimal SGD amounts), use ``canonical_money_str()``
    instead.
    """
    if not isinstance(amount, Decimal):
        raise MoneyValidationError(
            f"canonical_decimal_str requires Decimal, got {type(amount).__name__}"
        )
    if not amount.is_finite():
        raise MoneyValidationError(
            f"canonical_decimal_str requires a finite Decimal, got {amount!r}"
        )
    # Strip trailing zeros and normalise.
    normalized = amount.normalize()
    # Canonicalise negative zero to plain zero.
    if normalized == Decimal("-0"):
        return "0"

    # Build a plain-decimal string from the sign/digits/exponent tuple.
    # This avoids scientific notation that Decimal.normalize() can produce
    # for values like Decimal("100.0") → "1E+2".
    sign_tuple, digits, exponent = normalized.as_tuple()
    sign: int = 1 if sign_tuple else 0  # 1 = negative, 0 = positive/zero
    body = "".join(str(d) for d in digits)

    exp: int = int(exponent) if isinstance(exponent, int) else 0
    if exp > 0:
        body += "0" * exp
    elif exp < 0:
        neg_exp = -exp
        if neg_exp >= len(body):
            body = "0." + "0" * (neg_exp - len(body)) + body
        else:
            dot_pos = len(body) - neg_exp
            body = body[:dot_pos] + "." + body[dot_pos:]

    if sign:
        body = "-" + body
    return body


def canonical_money_str(amount: Decimal, currency: str) -> str:
    """Serialize *amount* at the currency's canonical minor-unit scale.

    Unlike ``canonical_decimal_str``, this preserves the currency-specific
    number of decimal places so that the output always reflects the
    currency's minor-unit resolution::

        canonical_money_str(Decimal("12.3"), "SGD") -> "12.30"
        canonical_money_str(Decimal("100"), "JPY")  -> "100"

    This is appropriate for display-oriented or currency-specific snapshot
    fields where scale must be preserved.
    """
    if not isinstance(amount, Decimal):
        raise MoneyValidationError(
            f"canonical_money_str requires Decimal, got {type(amount).__name__}"
        )
    if not amount.is_finite():
        raise MoneyValidationError(f"canonical_money_str requires a finite Decimal, got {amount!r}")
    return str(quantize_for_currency(amount, currency))


# ---------------------------------------------------------------------------
# Money value object (immutable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Money:
    """An immutable monetary value with explicit currency.

    This is the canonical value object for monetary amounts in the
    application.  It bundles amount + currency so that same-currency
    rules are enforced by the type itself.

    Construction accepts Decimal or canonical string amounts; floats
    and other unsafe inputs are rejected.

    Usage::

        price = Money(Decimal("12.30"), "SGD")
        price = Money.from_string("12.30", "SGD")
    """

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        # Validate via the shared contract.
        object.__setattr__(self, "amount", money_decimal(self.amount, label="Money.amount"))
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        # Validate amount does not exceed currency scale.
        validate_amount_for_currency(self.amount, self.currency, label="Money.amount")

    @classmethod
    def from_string(cls, amount_str: str, currency: str) -> Money:
        """Construct from a canonical decimal string."""
        return cls(money_decimal(amount_str, label="Money amount string"), currency)

    @classmethod
    def zero(cls, currency: str) -> Money:
        """Convenience constructor for zero in a given currency."""
        return cls(ZERO, currency)

    # -- comparison ---------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self.amount == other.amount and self.currency == other.currency

    def __lt__(self, other: Money) -> bool:
        self._require_same_currency(other, "compare")
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._require_same_currency(other, "compare")
        return self.amount <= other.amount

    # -- arithmetic ---------------------------------------------------------

    def __add__(self, other: Money) -> Money:
        self._require_same_currency(other, "add")
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._require_same_currency(other, "subtract")
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    def __mul__(self, scalar: int | Decimal) -> Money:  # type: ignore[override]
        if not isinstance(scalar, (int, Decimal)):
            return NotImplemented
        return Money(self.amount * Decimal(scalar), self.currency)

    # -- helpers ------------------------------------------------------------

    def _require_same_currency(self, other: Money, operation: str) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"Cannot {operation} {self.currency} and {other.currency}: "
                f"cross-currency operations are not supported"
            )

    def is_zero(self) -> bool:
        return self.amount == ZERO

    def is_positive(self) -> bool:
        return self.amount > ZERO

    def is_negative(self) -> bool:
        return self.amount < ZERO

    def canonical_str(self) -> str:
        """Serialize amount as canonical decimal string."""
        return canonical_decimal_str(self.amount)

    def __repr__(self) -> str:
        return f"Money({self.amount!r}, {self.currency!r})"


__all__ = [
    "MONEY_QUANTUM",
    "ZERO",
    "SUPPORTED_CURRENCIES",
    "MoneyValidationError",
    "CurrencyMismatchError",
    "normalize_currency",
    "minor_units",
    "money_decimal",
    "validate_amount_for_currency",
    "quantize_for_currency",
    "quantum_for_currency",
    "SignPolicy",
    "require_same_currency",
    "canonical_decimal_str",
    "canonical_money_str",
    "Money",
]
