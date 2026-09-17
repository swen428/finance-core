"""Tests for the unified Money Contract (finance_core/money.py).

Covers:
- Decimal validation and type safety
- Currency normalization and validation
- Minor-unit rules
- Amount scale / precision validation
- Sign policies
- Same-currency assertions
- Canonical serialization
- Money value object
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from finance_core.money import (
    SUPPORTED_CURRENCIES,
    ZERO,
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
    require_same_currency,
    validate_amount_for_currency,
)

# ============================================================================
# money_decimal — authoritative Decimal conversion
# ============================================================================


class TestMoneyDecimal:
    """Authoritative Decimal conversion — accepted and rejected inputs."""

    # -- accepted inputs ----------------------------------------------------

    @pytest.mark.parametrize(
        "value,expected",
        [
            (Decimal("12.30"), Decimal("12.30")),
            (Decimal("0"), Decimal("0")),
            (Decimal("-5.00"), Decimal("-5.00")),
            (Decimal("0.01"), Decimal("0.01")),
            (42, Decimal("42")),
            (0, Decimal("0")),
            (-7, Decimal("-7")),
            ("12.30", Decimal("12.30")),
            ("0", Decimal("0")),
            ("0.00", Decimal("0.00")),
            ("-5.00", Decimal("-5.00")),
            ("1234567890.12", Decimal("1234567890.12")),
        ],
    )
    def test_accepts_valid_input(self, value, expected):
        result = money_decimal(value)
        assert result == expected
        assert isinstance(result, Decimal)

    def test_accepts_decimal(self):
        assert money_decimal(Decimal("100.00")) == Decimal("100.00")

    def test_accepts_int(self):
        assert money_decimal(42) == Decimal("42")

    def test_accepts_canonical_string(self):
        assert money_decimal("12.30") == Decimal("12.30")

    # -- rejected: float ----------------------------------------------------

    def test_rejects_float(self):
        with pytest.raises(MoneyValidationError, match="float"):
            money_decimal(0.1)

    def test_rejects_float_zero(self):
        with pytest.raises(MoneyValidationError, match="float"):
            money_decimal(0.0)

    def test_rejects_float_negative(self):
        with pytest.raises(MoneyValidationError, match="float"):
            money_decimal(-1.5)

    # -- rejected: bool -----------------------------------------------------

    def test_rejects_bool_true(self):
        with pytest.raises(MoneyValidationError, match="boolean"):
            money_decimal(True)

    def test_rejects_bool_false(self):
        with pytest.raises(MoneyValidationError, match="boolean"):
            money_decimal(False)

    # -- rejected: None -----------------------------------------------------

    def test_rejects_none(self):
        with pytest.raises(MoneyValidationError, match="None"):
            money_decimal(None)

    # -- rejected: malformed strings ----------------------------------------

    def test_rejects_empty_string(self):
        with pytest.raises(MoneyValidationError, match="empty"):
            money_decimal("")

    def test_rejects_whitespace_only_string(self):
        with pytest.raises(MoneyValidationError, match="empty"):
            money_decimal("   ")

    def test_rejects_garbage_string(self):
        with pytest.raises(MoneyValidationError, match="not a valid Decimal"):
            money_decimal("not-a-number")

    def test_rejects_random_text(self):
        with pytest.raises(MoneyValidationError, match="not a valid Decimal"):
            money_decimal("hello world")

    # -- rejected: scientific notation --------------------------------------

    def test_rejects_scientific_lowercase(self):
        with pytest.raises(MoneyValidationError, match="scientific notation"):
            money_decimal("1.5e2")

    def test_rejects_scientific_uppercase(self):
        with pytest.raises(MoneyValidationError, match="scientific notation"):
            money_decimal("1.5E+2")

    def test_rejects_scientific_negative(self):
        with pytest.raises(MoneyValidationError, match="scientific notation"):
            money_decimal("1.5e-3")

    # -- rejected: NaN and Infinity -----------------------------------------

    def test_rejects_decimal_nan(self):
        with pytest.raises(MoneyValidationError, match="finite"):
            money_decimal(Decimal("NaN"))

    def test_rejects_decimal_infinity(self):
        with pytest.raises(MoneyValidationError, match="finite"):
            money_decimal(Decimal("Infinity"))

    def test_rejects_decimal_negative_infinity(self):
        with pytest.raises(MoneyValidationError, match="finite"):
            money_decimal(Decimal("-Infinity"))

    def test_rejects_float_nan(self):
        with pytest.raises(MoneyValidationError, match="float"):
            money_decimal(float("nan"))

    def test_rejects_float_inf(self):
        with pytest.raises(MoneyValidationError, match="float"):
            money_decimal(float("inf"))

    def test_rejects_float_neg_inf(self):
        with pytest.raises(MoneyValidationError, match="float"):
            money_decimal(float("-inf"))

    # -- rejected: wrong types ----------------------------------------------

    def test_rejects_list(self):
        with pytest.raises(MoneyValidationError):
            money_decimal([1, 2, 3])

    def test_rejects_dict(self):
        with pytest.raises(MoneyValidationError):
            money_decimal({"amount": 10})

    # -- label in error messages --------------------------------------------

    def test_includes_label_in_error(self):
        with pytest.raises(MoneyValidationError, match="item price"):
            money_decimal(0.1, label="item price")


# ============================================================================
# normalize_currency
# ============================================================================


class TestNormalizeCurrency:
    """Currency normalization and validation."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("SGD", "SGD"),
            ("sgd", "SGD"),
            ("Sgd", "SGD"),
            ("usd", "USD"),
            ("eur", "EUR"),
            ("jpy", "JPY"),
        ],
    )
    def test_normalizes_case(self, raw, expected):
        assert normalize_currency(raw) == expected

    def test_strips_whitespace(self):
        assert normalize_currency("  SGD  ") == "SGD"

    def test_rejects_empty(self):
        with pytest.raises(MoneyValidationError, match="empty"):
            normalize_currency("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(MoneyValidationError, match="empty"):
            normalize_currency("   ")

    def test_rejects_non_string(self):
        with pytest.raises(MoneyValidationError, match="string"):
            normalize_currency(123)

    def test_rejects_too_short(self):
        with pytest.raises(MoneyValidationError, match="3 characters"):
            normalize_currency("SG")

    def test_rejects_too_long(self):
        with pytest.raises(MoneyValidationError, match="3 characters"):
            normalize_currency("SGDD")

    def test_rejects_digits(self):
        with pytest.raises(MoneyValidationError):
            normalize_currency("S1D")

    @pytest.mark.parametrize("bad", ["123", "S1D", "AB1"])
    def test_rejects_non_alpha(self, bad):
        with pytest.raises(MoneyValidationError):
            normalize_currency(bad)

    def test_rejects_unsupported_currency(self):
        with pytest.raises(MoneyValidationError, match="Unsupported"):
            normalize_currency("XYZ")

    def test_rejects_myr_unsupported(self):
        with pytest.raises(MoneyValidationError, match="Unsupported"):
            normalize_currency("MYR")


# ============================================================================
# minor_units
# ============================================================================


class TestMinorUnits:
    """Minor-unit decimal places for supported currencies."""

    @pytest.mark.parametrize(
        "currency,expected",
        [
            ("SGD", 2),
            ("USD", 2),
            ("EUR", 2),
            ("GBP", 2),
            ("AUD", 2),
            ("CNY", 2),
            ("HKD", 2),
            ("JPY", 0),
        ],
    )
    def test_correct_minor_units(self, currency, expected):
        assert minor_units(currency) == expected

    def test_case_insensitive(self):
        assert minor_units("sgd") == 2

    def test_rejects_unsupported(self):
        with pytest.raises(MoneyValidationError):
            minor_units("XYZ")


# ============================================================================
# validate_amount_for_currency
# ============================================================================


class TestValidateAmountForCurrency:
    """Amount scale validation against currency minor units."""

    def test_accepts_two_decimal_places_for_sgd(self):
        validate_amount_for_currency(Decimal("12.30"), "SGD")

    def test_accepts_one_decimal_place_for_sgd(self):
        validate_amount_for_currency(Decimal("12.3"), "SGD")

    def test_accepts_integer_for_sgd(self):
        validate_amount_for_currency(Decimal("12"), "SGD")

    def test_accepts_zero(self):
        validate_amount_for_currency(Decimal("0"), "SGD")

    def test_accepts_zero_decimal_places_for_jpy(self):
        validate_amount_for_currency(Decimal("100"), "JPY")

    def test_rejects_three_decimal_places_for_sgd(self):
        with pytest.raises(MoneyValidationError, match="precision"):
            validate_amount_for_currency(Decimal("12.345"), "SGD")

    def test_rejects_one_decimal_place_for_jpy(self):
        with pytest.raises(MoneyValidationError, match="precision"):
            validate_amount_for_currency(Decimal("100.5"), "JPY")

    def test_returns_amount_on_success(self):
        amount = Decimal("12.30")
        assert validate_amount_for_currency(amount, "SGD") is amount

    def test_label_in_error(self):
        with pytest.raises(MoneyValidationError, match="net paid"):
            validate_amount_for_currency(Decimal("12.345"), "SGD", label="net paid")

    def test_does_not_silently_round(self):
        """Sub-minor-unit precision must be rejected, not rounded."""
        with pytest.raises(MoneyValidationError):
            validate_amount_for_currency(Decimal("12.349"), "SGD")


# ============================================================================
# quantize_for_currency
# ============================================================================


class TestQuantizeForCurrency:
    """Explicit quantization to currency minor units."""

    def test_quantize_rounds_half_up(self):
        assert quantize_for_currency(Decimal("12.345"), "SGD") == Decimal("12.35")

    def test_quantize_rounds_down(self):
        assert quantize_for_currency(Decimal("12.344"), "SGD") == Decimal("12.34")

    def test_quantize_exact_no_change(self):
        assert quantize_for_currency(Decimal("12.30"), "SGD") == Decimal("12.30")

    def test_quantize_jpy_zero_places(self):
        assert quantize_for_currency(Decimal("100.6"), "JPY") == Decimal("101")


# ============================================================================
# SignPolicy
# ============================================================================


class TestSignPolicyAny:
    """SignPolicy.ANY — accepts all finite values."""

    def test_accepts_positive(self):
        SignPolicy.ANY.enforce(Decimal("10.00"), label="test")

    def test_accepts_zero(self):
        SignPolicy.ANY.enforce(Decimal("0.00"), label="test")

    def test_accepts_negative(self):
        SignPolicy.ANY.enforce(Decimal("-10.00"), label="test")

    def test_returns_amount(self):
        amount = Decimal("10.00")
        assert SignPolicy.ANY.enforce(amount, label="test") is amount


class TestSignPolicyNonNegative:
    """SignPolicy.NON_NEGATIVE — rejects negative values."""

    def test_accepts_positive(self):
        SignPolicy.NON_NEGATIVE.enforce(Decimal("10.00"), label="test")

    def test_accepts_zero(self):
        SignPolicy.NON_NEGATIVE.enforce(Decimal("0.00"), label="test")

    def test_accepts_negative_zero(self):
        """Negative zero should canonicalize to zero and be accepted."""
        # money_decimal would reject float, but Decimal("-0.00") is a thing.
        # The sign policy compares against ZERO (0.00).
        SignPolicy.NON_NEGATIVE.enforce(Decimal("-0.00"), label="test")

    def test_rejects_negative(self):
        with pytest.raises(MoneyValidationError, match="non-negative"):
            SignPolicy.NON_NEGATIVE.enforce(Decimal("-10.00"), label="test")

    def test_label_in_error(self):
        with pytest.raises(MoneyValidationError, match="item price"):
            SignPolicy.NON_NEGATIVE.enforce(Decimal("-5.00"), label="item price")


class TestSignPolicyStrictlyPositive:
    """SignPolicy.STRICTLY_POSITIVE — rejects zero and negative."""

    def test_accepts_positive(self):
        SignPolicy.STRICTLY_POSITIVE.enforce(Decimal("10.00"), label="test")

    def test_rejects_zero(self):
        with pytest.raises(MoneyValidationError, match="strictly positive"):
            SignPolicy.STRICTLY_POSITIVE.enforce(Decimal("0.00"), label="test")

    def test_rejects_negative_zero(self):
        with pytest.raises(MoneyValidationError, match="strictly positive"):
            SignPolicy.STRICTLY_POSITIVE.enforce(Decimal("-0.00"), label="test")

    def test_rejects_negative(self):
        with pytest.raises(MoneyValidationError, match="strictly positive"):
            SignPolicy.STRICTLY_POSITIVE.enforce(Decimal("-5.00"), label="test")


# ============================================================================
# require_same_currency
# ============================================================================


class TestRequireSameCurrency:
    """Same-currency assertions."""

    def test_same_currency_passes(self):
        require_same_currency("SGD", "SGD")

    def test_same_currency_case_insensitive(self):
        require_same_currency("sgd", "SGD")

    def test_different_currency_fails(self):
        with pytest.raises(CurrencyMismatchError, match="cross-currency"):
            require_same_currency("SGD", "USD")

    def test_label_in_error(self):
        with pytest.raises(CurrencyMismatchError, match="receipt"):
            require_same_currency("SGD", "USD", label_a="receipt currency", label_b="item currency")


# ============================================================================
# canonical_decimal_str
# ============================================================================


class TestCanonicalDecimalStr:
    """Canonical decimal string serialization — normalised form."""

    # -- basic normalisation ------------------------------------------------

    @pytest.mark.parametrize(
        "value,expected",
        [
            (Decimal("12.30"), "12.3"),
            (Decimal("12.300"), "12.3"),
            (Decimal("12.3"), "12.3"),
            (Decimal("12.000"), "12"),
            (Decimal("0.00"), "0"),
            (Decimal("0"), "0"),
            (Decimal("-0.00"), "0"),
            (Decimal("0.000"), "0"),
            (Decimal("100"), "100"),
            (Decimal("100.0"), "100"),
            (Decimal("100.00"), "100"),
            (Decimal("-5.00"), "-5"),
            (Decimal("0.01"), "0.01"),
            (Decimal("0.10"), "0.1"),
            (Decimal("0.000001"), "0.000001"),
            (Decimal("1234567890.12"), "1234567890.12"),
        ],
    )
    def test_normalises_to_canonical_form(self, value, expected):
        """Equivalent Decimal values serialise identically."""
        assert canonical_decimal_str(value) == expected

    # -- determinism -------------------------------------------------------

    def test_equivalent_values_produce_same_string(self):
        a = canonical_decimal_str(Decimal("12.300"))
        b = canonical_decimal_str(Decimal("12.3"))
        c = canonical_decimal_str(Decimal("12.30"))
        assert a == b == c

    def test_deterministic_across_repeated_calls(self):
        for _ in range(5):
            assert canonical_decimal_str(Decimal("12.300")) == "12.3"

    # -- zero and negative zero --------------------------------------------

    def test_zero_is_zero(self):
        assert canonical_decimal_str(Decimal("0")) == "0"
        assert canonical_decimal_str(Decimal("0.00")) == "0"
        assert canonical_decimal_str(Decimal("0.000")) == "0"

    def test_negative_zero_is_canonical_zero(self):
        assert canonical_decimal_str(Decimal("-0.00")) == "0"
        assert canonical_decimal_str(Decimal("-0.0")) == "0"

    # -- integer values ----------------------------------------------------

    def test_integer_values_no_decimal_suffix(self):
        assert canonical_decimal_str(Decimal("100")) == "100"
        assert canonical_decimal_str(Decimal("42")) == "42"
        assert canonical_decimal_str(Decimal("-5")) == "-5"

    # -- no scientific notation --------------------------------------------

    def test_no_scientific_notation_in_output(self):
        """Large values must not produce scientific notation."""
        result = canonical_decimal_str(Decimal("12345678901234.56"))
        assert "e" not in result.lower()

    # -- rejects -----------------------------------------------------------

    def test_rejects_non_decimal(self):
        with pytest.raises(MoneyValidationError):
            canonical_decimal_str("12.30")  # type: ignore[arg-type]

    def test_rejects_nan(self):
        with pytest.raises(MoneyValidationError, match="finite"):
            canonical_decimal_str(Decimal("NaN"))

    def test_rejects_infinity(self):
        with pytest.raises(MoneyValidationError, match="finite"):
            canonical_decimal_str(Decimal("Infinity"))

    def test_rejects_negative_infinity(self):
        with pytest.raises(MoneyValidationError, match="finite"):
            canonical_decimal_str(Decimal("-Infinity"))


# ============================================================================
# canonical_money_str
# ============================================================================


class TestCanonicalMoneyStr:
    """Currency-aware canonical monetary string serialization."""

    def test_sgd_preserves_two_decimal_places(self):
        assert canonical_money_str(Decimal("12.3"), "SGD") == "12.30"
        assert canonical_money_str(Decimal("12.30"), "SGD") == "12.30"

    def test_jpy_produces_whole_yen(self):
        assert canonical_money_str(Decimal("100"), "JPY") == "100"
        assert canonical_money_str(Decimal("100.0"), "JPY") == "100"

    def test_zero_is_currency_aware(self):
        assert canonical_money_str(Decimal("0"), "SGD") == "0.00"
        assert canonical_money_str(Decimal("0"), "JPY") == "0"

    def test_rejects_non_finite(self):
        with pytest.raises(MoneyValidationError, match="finite"):
            canonical_money_str(Decimal("NaN"), "SGD")

    def test_rejects_non_decimal(self):
        with pytest.raises(MoneyValidationError):
            canonical_money_str("12.30", "SGD")  # type: ignore[arg-type]


# ============================================================================
# Money value object
# ============================================================================


class TestMoneyValueObject:
    """Immutable Money value object."""

    # -- construction -------------------------------------------------------

    def test_construct_from_decimal(self):
        m = Money(Decimal("12.30"), "SGD")
        assert m.amount == Decimal("12.30")
        assert m.currency == "SGD"

    def test_construct_from_string(self):
        m = Money.from_string("12.30", "SGD")
        assert m.amount == Decimal("12.30")

    def test_construct_zero(self):
        m = Money.zero("SGD")
        assert m.amount == ZERO
        assert m.currency == "SGD"

    def test_normalizes_currency_case(self):
        m = Money(Decimal("10.00"), "sgd")
        assert m.currency == "SGD"

    def test_rejects_float_amount(self):
        with pytest.raises(MoneyValidationError, match="float"):
            Money(0.1, "SGD")  # type: ignore[arg-type]

    def test_rejects_bool_amount(self):
        with pytest.raises(MoneyValidationError, match="boolean"):
            Money(True, "SGD")  # type: ignore[arg-type]

    def test_rejects_unsupported_currency(self):
        with pytest.raises(MoneyValidationError, match="Unsupported"):
            Money(Decimal("10.00"), "XYZ")

    def test_rejects_sub_minor_unit_precision(self):
        with pytest.raises(MoneyValidationError, match="precision"):
            Money(Decimal("12.345"), "SGD")

    def test_accepts_jpy_zero_places(self):
        m = Money(Decimal("100"), "JPY")
        assert m.amount == Decimal("100")

    def test_rejects_jpy_with_sub_unit(self):
        with pytest.raises(MoneyValidationError, match="precision"):
            Money(Decimal("100.5"), "JPY")

    # -- equality -----------------------------------------------------------

    def test_equal_same_amount_and_currency(self):
        assert Money(Decimal("10.00"), "SGD") == Money(Decimal("10.00"), "SGD")

    def test_not_equal_different_amount(self):
        assert Money(Decimal("10.00"), "SGD") != Money(Decimal("20.00"), "SGD")

    def test_not_equal_different_currency_same_numeric(self):
        """Same numeric value, different currency — not equal."""
        assert Money(Decimal("10.00"), "SGD") != Money(Decimal("10.00"), "USD")

    def test_not_equal_non_money(self):
        assert Money(Decimal("10.00"), "SGD") != Decimal("10.00")

    # -- comparison ---------------------------------------------------------

    def test_less_than(self):
        assert Money(Decimal("5.00"), "SGD") < Money(Decimal("10.00"), "SGD")

    def test_less_than_different_currency_fails(self):
        with pytest.raises(CurrencyMismatchError):
            Money(Decimal("5.00"), "SGD") < Money(Decimal("10.00"), "USD")

    # -- arithmetic ---------------------------------------------------------

    def test_add_same_currency(self):
        result = Money(Decimal("5.00"), "SGD") + Money(Decimal("10.00"), "SGD")
        assert result == Money(Decimal("15.00"), "SGD")

    def test_add_different_currency_fails(self):
        with pytest.raises(CurrencyMismatchError):
            Money(Decimal("5.00"), "SGD") + Money(Decimal("10.00"), "USD")

    def test_subtract_same_currency(self):
        result = Money(Decimal("15.00"), "SGD") - Money(Decimal("5.00"), "SGD")
        assert result == Money(Decimal("10.00"), "SGD")

    def test_subtract_different_currency_fails(self):
        with pytest.raises(CurrencyMismatchError):
            Money(Decimal("15.00"), "SGD") - Money(Decimal("5.00"), "USD")

    def test_neg(self):
        result = -Money(Decimal("10.00"), "SGD")
        assert result == Money(Decimal("-10.00"), "SGD")

    def test_abs_positive(self):
        result = abs(Money(Decimal("10.00"), "SGD"))
        assert result == Money(Decimal("10.00"), "SGD")

    def test_abs_negative(self):
        result = abs(Money(Decimal("-10.00"), "SGD"))
        assert result == Money(Decimal("10.00"), "SGD")

    def test_mul_int(self):
        result = Money(Decimal("10.00"), "SGD") * 3
        assert result == Money(Decimal("30.00"), "SGD")

    def test_mul_decimal_scalar(self):
        result = Money(Decimal("10.00"), "SGD") * Decimal("1.5")
        assert result == Money(Decimal("15.00"), "SGD")

    # -- helpers ------------------------------------------------------------

    def test_is_zero(self):
        assert Money(Decimal("0.00"), "SGD").is_zero()
        assert not Money(Decimal("10.00"), "SGD").is_zero()

    def test_is_positive(self):
        assert Money(Decimal("10.00"), "SGD").is_positive()
        assert not Money(Decimal("0.00"), "SGD").is_positive()
        assert not Money(Decimal("-10.00"), "SGD").is_positive()

    def test_is_negative(self):
        assert Money(Decimal("-10.00"), "SGD").is_negative()
        assert not Money(Decimal("0.00"), "SGD").is_negative()

    def test_canonical_str_uses_normalised_form(self):
        assert Money(Decimal("12.30"), "SGD").canonical_str() == "12.3"
        assert Money(Decimal("12.300"), "SGD").canonical_str() == "12.3"

    # -- immutability -------------------------------------------------------

    def test_immutable(self):
        m = Money(Decimal("10.00"), "SGD")
        with pytest.raises(Exception):
            m.amount = Decimal("20.00")  # type: ignore[misc]

    def test_repr(self):
        m = Money(Decimal("12.30"), "SGD")
        assert "Money" in repr(m)
        assert "12.30" in repr(m)
        assert "SGD" in repr(m)


# ============================================================================
# Cross-currency rejection
# ============================================================================


class TestCrossCurrencyRejection:
    """Cross-currency operations must fail."""

    def test_require_same_currency_rejects_mismatch(self):
        with pytest.raises(CurrencyMismatchError):
            require_same_currency("SGD", "USD")

    def test_money_addition_cross_currency(self):
        with pytest.raises(CurrencyMismatchError):
            Money(Decimal("10.00"), "SGD") + Money(Decimal("5.00"), "USD")

    def test_money_subtraction_cross_currency(self):
        with pytest.raises(CurrencyMismatchError):
            Money(Decimal("10.00"), "SGD") - Money(Decimal("5.00"), "USD")

    def test_equal_numeric_different_currency_not_equivalent(self):
        """10 SGD is not financially equivalent to 10 USD."""
        a = Money(Decimal("10.00"), "SGD")
        b = Money(Decimal("10.00"), "USD")
        assert a != b

    def test_require_same_currency_passes_same(self):
        """Same currency (case-insensitive) must pass."""
        require_same_currency("SGD", "sgd")


# ============================================================================
# Parameterized supported currencies
# ============================================================================


class TestSupportedCurrencyCoverage:
    """Every supported currency has correct minor units and validates."""

    @pytest.mark.parametrize(
        "currency,expected_minor_units",
        [
            ("SGD", 2),
            ("USD", 2),
            ("EUR", 2),
            ("GBP", 2),
            ("AUD", 2),
            ("CNY", 2),
            ("HKD", 2),
            ("JPY", 0),
        ],
    )
    def test_minor_units(self, currency, expected_minor_units):
        assert minor_units(currency) == expected_minor_units

    @pytest.mark.parametrize("currency", sorted(SUPPORTED_CURRENCIES))
    def test_can_construct_money_zero(self, currency):
        m = Money.zero(currency)
        assert m.amount == ZERO
        assert m.currency == currency

    @pytest.mark.parametrize("currency", sorted(SUPPORTED_CURRENCIES))
    def test_normalize_accepts(self, currency):
        assert normalize_currency(currency) == currency
        assert normalize_currency(currency.lower()) == currency


# ============================================================================
# Scale boundaries
# ============================================================================


class TestScaleBoundaries:
    """Scale validation at minor-unit boundaries."""

    @pytest.mark.parametrize(
        "amount_str,currency,should_pass",
        [
            ("12", "SGD", True),
            ("12.3", "SGD", True),
            ("12.30", "SGD", True),
            ("12.34", "SGD", True),
            ("12.345", "SGD", False),  # sub-minor-unit
            ("12.3456", "SGD", False),  # sub-minor-unit
            ("100", "JPY", True),
            ("100.0", "JPY", True),  # numerically equal to 100
            ("100.00", "JPY", True),  # numerically equal to 100
            ("100.5", "JPY", False),  # has sub-unit precision for JPY
        ],
    )
    def test_validate_amount_for_currency(self, amount_str, currency, should_pass):
        amount = Decimal(amount_str)
        if should_pass:
            validate_amount_for_currency(amount, currency)
        else:
            with pytest.raises(MoneyValidationError):
                validate_amount_for_currency(amount, currency)


# ============================================================================
# Negative zero handling
# ============================================================================


class TestNegativeZero:
    """Negative zero must be handled consistently."""

    def test_money_from_negative_zero_decimal(self):
        """Decimal('-0.00') should be accepted as zero equivalent."""
        m = Money(Decimal("-0.00"), "SGD")
        assert m.amount == ZERO
        assert m.is_zero()

    def test_non_negative_policy_accepts_negative_zero(self):
        SignPolicy.NON_NEGATIVE.enforce(Decimal("-0.00"), label="test")

    def test_strictly_positive_rejects_negative_zero(self):
        with pytest.raises(MoneyValidationError, match="strictly positive"):
            SignPolicy.STRICTLY_POSITIVE.enforce(Decimal("-0.00"), label="test")
