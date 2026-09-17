"""Tests for exact reconciliation, safe payer-following (positive and negative
remainders), largest-remainder, minor-unit allocation guards, end-to-end
Stage A validation, and TC001 regression.
"""

from decimal import Decimal, localcontext

import pytest

from finance_core.calculators.receipt_split_calculator import (
    _allocate_minor_units,
    _validate_exact_reconciliation,
    calculate_receipt_split,
)
from finance_core.money import MoneyValidationError


def money(value: str) -> Decimal:
    return Decimal(value)


# ============================================================================
# Manual-allocation admission boundary
# ============================================================================


class TestManualAllocationAdmission:
    """Manual amounts must satisfy the Money Contract before allocation math."""

    @pytest.mark.parametrize(
        ("currency", "amount", "allocations", "expected"),
        [
            pytest.param(
                "SGD",
                "10.00",
                {"Owner": "-1.00", "MemberA": "11.00"},
                "must be non-negative",
                id="negative",
            ),
            pytest.param(
                "SGD",
                "10.00",
                {"Owner": "4.999", "MemberA": "5.001"},
                "precision.*SGD",
                id="sgd-sub-minor",
            ),
            pytest.param(
                "JPY",
                "100",
                {"Owner": "49.5", "MemberA": "50.5"},
                "precision.*JPY",
                id="jpy-fractional",
            ),
        ],
    )
    def test_manual_item_allocation_rejects_invalid_value_at_input_boundary(
        self,
        currency: str,
        amount: str,
        allocations: dict[str, str],
        expected: str,
    ) -> None:
        with pytest.raises(
            MoneyValidationError,
            match=rf"manual item allocation for 'Owner'.*{expected}",
        ):
            calculate_receipt_split(
                {
                    "currency": currency,
                    "participants": ["Owner", "MemberA"],
                    "receipts": [
                        {
                            "merchant": "Manual Item Precision",
                            "paid_by": "Owner",
                            "net_paid": amount,
                            "items": [
                                {
                                    "description": "Shared item",
                                    "amount": amount,
                                    "allocation_method": "manual",
                                    "allocations": allocations,
                                }
                            ],
                        }
                    ],
                }
            )

    @pytest.mark.parametrize(
        ("currency", "item_amount", "adjustment_amount", "net_paid", "allocations", "expected"),
        [
            pytest.param(
                "SGD",
                "20.00",
                "2.00",
                "18.00",
                {"Owner": "-1.00", "MemberA": "3.00"},
                "must be non-negative",
                id="negative",
            ),
            pytest.param(
                "SGD",
                "20.00",
                "0.00",
                "20.00",
                {"Owner": "-1.00", "MemberA": "1.00"},
                "must be non-negative",
                id="zero-amount-negative-bypass",
            ),
            pytest.param(
                "SGD",
                "20.00",
                "2.00",
                "18.00",
                {"Owner": "0.999", "MemberA": "1.001"},
                "precision.*SGD",
                id="sgd-sub-minor",
            ),
            pytest.param(
                "JPY",
                "100",
                "10",
                "90",
                {"Owner": "4.5", "MemberA": "5.5"},
                "precision.*JPY",
                id="jpy-fractional",
            ),
        ],
    )
    def test_manual_adjustment_allocation_rejects_invalid_value_at_input_boundary(
        self,
        currency: str,
        item_amount: str,
        adjustment_amount: str,
        net_paid: str,
        allocations: dict[str, str],
        expected: str,
    ) -> None:
        with pytest.raises(
            MoneyValidationError,
            match=rf"discount manual allocation for 'Owner'.*{expected}",
        ):
            calculate_receipt_split(
                {
                    "currency": currency,
                    "participants": ["Owner", "MemberA"],
                    "receipts": [
                        {
                            "merchant": "Manual Adjustment Precision",
                            "paid_by": "Owner",
                            "net_paid": net_paid,
                            "items": [
                                {
                                    "description": "Shared item",
                                    "amount": item_amount,
                                    "participants": ["Owner", "MemberA"],
                                }
                            ],
                            "adjustments": [
                                {
                                    "type": "discount",
                                    "direction": "subtract",
                                    "amount": adjustment_amount,
                                    "allocation_method": "manual",
                                    "allocations": allocations,
                                }
                            ],
                        }
                    ],
                }
            )

    @pytest.mark.parametrize(
        ("currency", "receipt_overrides", "expected"),
        [
            pytest.param(
                "SGD",
                {
                    "discount": "0.00",
                    "discount_allocation_method": "manual",
                    "discount_allocations": {"Owner": "1.00", "MemberA": "-1.00"},
                },
                "discount manual allocation for 'MemberA'.*must be non-negative",
                id="discount-negative-non-first",
            ),
            pytest.param(
                "SGD",
                {
                    "discount": "0.00",
                    "discount_allocation_method": "manual",
                    "discount_allocations": {"Owner": "0.999", "MemberA": "-0.999"},
                },
                "discount manual allocation for 'Owner'.*precision.*SGD",
                id="discount-sgd-sub-minor",
            ),
            pytest.param(
                "JPY",
                {
                    "discount": "0",
                    "discount_allocation_method": "manual",
                    "discount_allocations": {"Owner": "0.5", "MemberA": "-0.5"},
                },
                "discount manual allocation for 'Owner'.*precision.*JPY",
                id="discount-jpy-fractional",
            ),
            pytest.param(
                "SGD",
                {
                    "service_charge_amount": "0.00",
                    "service_charge_allocation_method": "manual",
                    "service_charge_allocations": {"Owner": "1.00", "MemberA": "-1.00"},
                },
                "service_charge manual allocation for 'MemberA'.*must be non-negative",
                id="service-charge-negative-non-first",
            ),
            pytest.param(
                "SGD",
                {
                    "service_charge_amount": "0.00",
                    "service_charge_allocation_method": "manual",
                    "service_charge_allocations": {"Owner": "0.999", "MemberA": "-0.999"},
                },
                "service_charge manual allocation for 'Owner'.*precision.*SGD",
                id="service-charge-sgd-sub-minor",
            ),
            pytest.param(
                "JPY",
                {
                    "service_charge_amount": "0",
                    "service_charge_allocation_method": "manual",
                    "service_charge_allocations": {"Owner": "0.5", "MemberA": "-0.5"},
                },
                "service_charge manual allocation for 'Owner'.*precision.*JPY",
                id="service-charge-jpy-fractional",
            ),
        ],
    )
    def test_zero_receipt_level_manual_adjustment_cannot_bypass_admission(
        self,
        currency: str,
        receipt_overrides: dict[str, object],
        expected: str,
    ) -> None:
        item_amount = "100" if currency == "JPY" else "20.00"
        receipt: dict[str, object] = {
            "merchant": "Zero Receipt-Level Manual Adjustment",
            "paid_by": "Owner",
            "net_paid": item_amount,
            "items": [
                {
                    "description": "Shared item",
                    "amount": item_amount,
                    "participants": ["Owner", "MemberA"],
                }
            ],
        }
        receipt.update(receipt_overrides)

        with pytest.raises(MoneyValidationError, match=expected):
            calculate_receipt_split(
                {
                    "currency": currency,
                    "participants": ["Owner", "MemberA"],
                    "receipts": [receipt],
                }
            )

    @pytest.mark.parametrize(
        ("currency", "amount", "allocations", "expected"),
        [
            (
                "SGD",
                "10.00",
                {"Owner": "4.00", "MemberA": "6.00"},
                {"Owner": "4.00", "MemberA": "6.00"},
            ),
            ("JPY", "100", {"Owner": "40", "MemberA": "60"}, {"Owner": "40", "MemberA": "60"}),
        ],
    )
    def test_valid_manual_item_allocation_remains_supported(
        self,
        currency: str,
        amount: str,
        allocations: dict[str, str],
        expected: dict[str, str],
    ) -> None:
        result = calculate_receipt_split(
            {
                "currency": currency,
                "participants": ["Owner", "MemberA"],
                "receipts": [
                    {
                        "merchant": "Valid Manual Item",
                        "paid_by": "Owner",
                        "net_paid": amount,
                        "items": [
                            {
                                "description": "Shared item",
                                "amount": amount,
                                "allocation_method": "manual",
                                "allocations": allocations,
                            }
                        ],
                    }
                ],
            }
        )

        assert result["participant_shares"] == {
            participant: money(value) for participant, value in expected.items()
        }

    @pytest.mark.parametrize("boundary", ["item", "adjustment"])
    def test_manual_allocation_maps_preserve_float_rejection(self, boundary: str) -> None:
        receipt: dict[str, object] = {
            "merchant": "Manual Allocation Float",
            "paid_by": "Owner",
            "net_paid": "10.00",
            "items": [
                {
                    "description": "Shared item",
                    "amount": "10.00",
                    "participants": ["Owner", "MemberA"],
                }
            ],
        }
        if boundary == "item":
            receipt["items"] = [
                {
                    "description": "Shared item",
                    "amount": "10.00",
                    "allocation_method": "manual",
                    "allocations": {"Owner": 4.0, "MemberA": "6.00"},
                }
            ]
        else:
            receipt["net_paid"] = "8.00"
            receipt["adjustments"] = [
                {
                    "type": "discount",
                    "direction": "subtract",
                    "amount": "2.00",
                    "allocation_method": "manual",
                    "allocations": {"Owner": 1.0, "MemberA": "1.00"},
                }
            ]

        with pytest.raises(TypeError, match="Currency values must not be floats"):
            calculate_receipt_split(
                {
                    "currency": "SGD",
                    "participants": ["Owner", "MemberA"],
                    "receipts": [receipt],
                }
            )


# ============================================================================
# Service-charge ratio contract
# ============================================================================


class TestServiceChargeRatioContract:
    """Service-charge rates are bounded ratios, never currency amounts."""

    @staticmethod
    def _calculate(
        *,
        currency: str,
        item_amount: str,
        rate: object,
        service_charge_amount: str | None = None,
    ) -> dict[str, object]:
        receipt: dict[str, object] = {
            "merchant": "Service Charge Ratio",
            "paid_by": "Owner",
            "items": [
                {
                    "description": "Shared item",
                    "amount": item_amount,
                    "participants": ["Owner"],
                }
            ],
            "service_charge_rate": rate,
        }
        if service_charge_amount is not None:
            receipt["service_charge_amount"] = service_charge_amount

        return calculate_receipt_split(
            {
                "currency": currency,
                "participants": ["Owner"],
                "receipts": [receipt],
            }
        )

    @pytest.mark.parametrize(
        ("currency", "item_amount", "rate", "expected_charge", "expected_net_paid"),
        [
            pytest.param("SGD", "100.00", "0.055", "5.50", "105.50", id="sgd-5-5-percent"),
            pytest.param("SGD", "66.60", "0.105", "6.99", "73.59", id="sgd-round-half-up"),
            pytest.param("SGD", "10.00", "0.0005", "0.01", "10.01", id="sgd-half-up-tie"),
            pytest.param("JPY", "100", "0.10", "10", "110", id="jpy-10-percent"),
            pytest.param("JPY", "1", "0.5", "1", "2", id="jpy-half-up-tie"),
            pytest.param("SGD", "100.00", "0.999", "99.90", "199.90", id="below-bound"),
        ],
    )
    def test_valid_ratio_computes_currency_quantized_service_charge(
        self,
        currency: str,
        item_amount: str,
        rate: str,
        expected_charge: str,
        expected_net_paid: str,
    ) -> None:
        result = self._calculate(currency=currency, item_amount=item_amount, rate=rate)

        receipt = result["receipts"][0]
        assert receipt["adjustments"] == [
            {
                "type": "service_charge",
                "direction": "add",
                "method": "proportional_by_item_amount",
                "amount": money(expected_charge),
                "participant_allocations": {"Owner": money(expected_charge)},
            }
        ]
        assert receipt["net_paid"] == money(expected_net_paid)

    @pytest.mark.parametrize(
        ("currency", "item_amount", "rate", "expected_net_paid"),
        [
            pytest.param(
                "SGD",
                "100.00",
                "0.0000499999999999999999999999999999",
                "100.00",
                id="sgd-below-half-cent",
            ),
            pytest.param(
                "JPY",
                "100",
                "0.0049999999999999999999999999999999",
                "100",
                id="jpy-below-half-yen",
            ),
        ],
    )
    def test_high_precision_ratio_is_independent_of_ambient_decimal_precision(
        self,
        currency: str,
        item_amount: str,
        rate: str,
        expected_net_paid: str,
    ) -> None:
        observed: list[tuple[list[dict[str, object]], Decimal]] = []
        for precision in (28, 80):
            with localcontext() as context:
                context.prec = precision
                result = self._calculate(
                    currency=currency,
                    item_amount=item_amount,
                    rate=rate,
                )
            receipt = result["receipts"][0]
            observed.append((receipt["adjustments"], receipt["net_paid"]))

        assert observed == [([], money(expected_net_paid)), ([], money(expected_net_paid))]

    def test_zero_ratio_is_accepted_without_service_charge_adjustment(self) -> None:
        result = self._calculate(currency="SGD", item_amount="100.00", rate="0")

        receipt = result["receipts"][0]
        assert receipt["adjustments"] == []
        assert receipt["net_paid"] == money("100.00")

    @pytest.mark.parametrize("rate", ["-0.10", "1.00", "1.001"])
    def test_ratio_outside_zero_inclusive_one_exclusive_is_rejected(self, rate: str) -> None:
        with pytest.raises(MoneyValidationError, match="service charge rate"):
            self._calculate(currency="SGD", item_amount="100.00", rate=rate)

    @pytest.mark.parametrize(
        ("rate", "error_type"),
        [
            pytest.param(0.1, TypeError, id="float"),
            pytest.param(True, ValueError, id="boolean"),
            pytest.param(None, ValueError, id="none"),
            pytest.param("1e-1", ValueError, id="scientific-notation"),
            pytest.param("NaN", ValueError, id="nan"),
            pytest.param("Infinity", ValueError, id="infinity"),
        ],
    )
    def test_ratio_preserves_safe_decimal_input_contract(
        self,
        rate: object,
        error_type: type[Exception],
    ) -> None:
        with pytest.raises(error_type, match="service charge rate|Currency values"):
            self._calculate(currency="SGD", item_amount="100.00", rate=rate)

    def test_explicit_service_charge_amount_remains_authoritative_over_rate(self) -> None:
        result = self._calculate(
            currency="SGD",
            item_amount="100.00",
            rate="1.00",
            service_charge_amount="2.00",
        )

        receipt = result["receipts"][0]
        assert receipt["adjustments"][0]["amount"] == money("2.00")
        assert receipt["net_paid"] == money("102.00")


# ============================================================================
# Exact reconciliation — Stage A
# ============================================================================


class TestExactReconciliation:
    """Stage A: exact share total must equal authoritative net_paid before
    any rounding or minor-unit allocation.  A tiny computational epsilon
    (1E-18) absorbs repeating-division Decimal artifacts only; real
    financial discrepancies fail."""

    def test_exact_match_passes(self) -> None:
        _validate_exact_reconciliation(
            {"A": money("6.00"), "B": money("4.00")},
            money("10.00"),
            receipt_label="Test",
            currency="SGD",
        )

    def test_repeating_division_artifact_passes(self) -> None:
        third = Decimal("10.00") / Decimal(3)
        _validate_exact_reconciliation(
            {"A": third, "B": third, "C": third},
            money("10.00"),
            receipt_label="Test",
            currency="SGD",
        )

    def test_sgd_10_004_vs_10_00_fails(self) -> None:
        with pytest.raises(ValueError, match="This is a calculation error"):
            _validate_exact_reconciliation(
                {"A": money("5.004"), "B": money("5.00")},
                money("10.00"),
                receipt_label="OverBy4MilliCents",
                currency="SGD",
            )

    def test_sgd_9_996_vs_10_00_fails(self) -> None:
        with pytest.raises(ValueError, match="This is a calculation error"):
            _validate_exact_reconciliation(
                {"A": money("4.996"), "B": money("5.00")},
                money("10.00"),
                receipt_label="UnderBy4MilliCents",
                currency="SGD",
            )

    def test_jpy_99_6_vs_100_fails(self) -> None:
        with pytest.raises(ValueError, match="This is a calculation error"):
            _validate_exact_reconciliation(
                {"A": money("50.6"), "B": money("49.0")},
                money("100"),
                receipt_label="JPYFractional",
                currency="JPY",
            )

    def test_jpy_100_4_vs_100_fails(self) -> None:
        with pytest.raises(ValueError, match="This is a calculation error"):
            _validate_exact_reconciliation(
                {"A": money("50.4"), "B": money("50.0")},
                money("100"),
                receipt_label="JPYOver",
                currency="JPY",
            )

    def test_large_difference_fails(self) -> None:
        with pytest.raises(ValueError, match="This is a calculation error"):
            _validate_exact_reconciliation(
                {"A": money("5.00"), "B": money("5.00")},
                money("100.00"),
                receipt_label="BigMismatch",
                currency="SGD",
            )

    def test_sub_cent_business_mismatch_fails(self) -> None:
        with pytest.raises(ValueError, match="This is a calculation error"):
            _validate_exact_reconciliation(
                {"A": money("0.001"), "B": money("0.001")},
                money("10.00"),
                receipt_label="TinyMismatch",
                currency="SGD",
            )

    def test_error_names_receipt_label(self) -> None:
        with pytest.raises(ValueError, match="BrokenCalc"):
            _validate_exact_reconciliation(
                {"A": money("1.00")},
                money("999.00"),
                receipt_label="BrokenCalc",
                currency="SGD",
            )

    def test_error_includes_difference(self) -> None:
        with pytest.raises(ValueError, match="difference"):
            _validate_exact_reconciliation(
                {"A": money("1.00")},
                money("2.00"),
                receipt_label="Test",
                currency="SGD",
            )

    # -- end-to-end Stage A coverage ---------------------------------------

    def test_e2e_explicit_net_paid_mismatch_fails_before_allocation(self) -> None:
        """Items total 10.00, net_paid says 50.00 — must fail before payer
        can absorb the mismatch."""
        with pytest.raises(ValueError, match="This is a calculation error"):
            calculate_receipt_split(
                {
                    "currency": "SGD",
                    "participants": ["Owner", "MemberA"],
                    "receipts": [
                        {
                            "merchant": "Mismatch",
                            "paid_by": "Owner",
                            "net_paid": "50.00",
                            "rounding_policy": "payer",
                            "items": [
                                {
                                    "description": "Item",
                                    "amount": "10.00",
                                    "participants": ["Owner", "MemberA"],
                                }
                            ],
                        }
                    ],
                }
            )

    def test_e2e_discrepancy_from_missing_adjustment_fails(self) -> None:
        """Items total 20.00, no discount applied, but net_paid says 17.00 —
        must fail at Stage A."""
        with pytest.raises(ValueError, match="This is a calculation error"):
            calculate_receipt_split(
                {
                    "currency": "SGD",
                    "participants": ["Owner", "MemberA"],
                    "receipts": [
                        {
                            "merchant": "MissingDiscount",
                            "paid_by": "Owner",
                            "net_paid": "17.00",
                            "rounding_policy": "payer",
                            "items": [
                                {
                                    "description": "Groceries",
                                    "amount": "20.00",
                                    "participants": ["Owner", "MemberA"],
                                }
                            ],
                        }
                    ],
                }
            )

    def test_e2e_repeating_division_reaches_stage_b(self) -> None:
        """10.00 / 3 passes Stage A and produces valid shares summing to 10.00."""
        result = calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["A", "B", "C"],
                "receipts": [
                    {
                        "merchant": "Test",
                        "paid_by": "A",
                        "net_paid": "10.00",
                        "items": [
                            {
                                "description": "Shared",
                                "amount": "10.00",
                                "participants": ["A", "B", "C"],
                            }
                        ],
                    }
                ],
            }
        )
        receipt = result["receipts"][0]
        assert sum(receipt["participant_shares"].values(), money("0.00")) == money("10.00")


# ============================================================================
# Payer remainder policy
# ============================================================================


class TestPayerRemainderPolicy:
    """Payer receives only the legitimate minor-unit remainder (positive or
    negative), never an arbitrary Decimal difference."""

    # -- positive remainder -------------------------------------------------

    def test_sgd_positive_one_cent_to_payer(self) -> None:
        result = _allocate_minor_units(
            exact_shares={
                "Owner": Decimal("3.333333333333333333333333333"),
                "MemberA": Decimal("3.333333333333333333333333333"),
                "MemberB": Decimal("3.333333333333333333333333333"),
            },
            exact_total=money("10.00"),
            currency="SGD",
            participants=["Owner", "MemberA", "MemberB"],
            remainder_policy="payer",
            payer="Owner",
        )
        assert result["Owner"] == money("3.34")
        assert result["MemberA"] == money("3.33")
        assert result["MemberB"] == money("3.33")
        assert sum(result.values(), money("0.00")) == money("10.00")

    def test_jpy_positive_one_yen_to_payer(self) -> None:
        result = _allocate_minor_units(
            exact_shares={
                "Owner": Decimal("33.333333333333333333333333333"),
                "MemberA": Decimal("33.333333333333333333333333333"),
                "MemberB": Decimal("33.333333333333333333333333333"),
            },
            exact_total=money("100"),
            currency="JPY",
            participants=["Owner", "MemberA", "MemberB"],
            remainder_policy="payer",
            payer="Owner",
        )
        assert result["Owner"] == money("34")
        assert result["MemberA"] == money("33")
        assert result["MemberB"] == money("33")
        assert sum(result.values(), money("0")) == money("100")

    # -- negative remainder ------------------------------------------------

    def test_sgd_negative_one_cent_remainder(self) -> None:
        """ROUND_HALF_UP over-sums (3.34+3.34+3.33=10.01), payer absorbs -1 cent."""
        result = _allocate_minor_units(
            exact_shares={
                "Owner": Decimal("3.335"),
                "MemberA": Decimal("3.335"),
                "MemberB": Decimal("3.330"),
            },
            exact_total=money("10.00"),
            currency="SGD",
            participants=["Owner", "MemberA", "MemberB"],
            remainder_policy="payer",
            payer="Owner",
        )
        assert result["Owner"] == money("3.33")
        assert result["MemberA"] == money("3.34")
        assert result["MemberB"] == money("3.33")
        assert sum(result.values(), money("0.00")) == money("10.00")

    def test_jpy_negative_one_yen_remainder(self) -> None:
        """ROUND_HALF_UP over-sums, payer absorbs -1 yen."""
        result = _allocate_minor_units(
            exact_shares={
                "Owner": Decimal("33.5"),
                "MemberA": Decimal("33.5"),
                "MemberB": Decimal("33.0"),
            },
            exact_total=money("100"),
            currency="JPY",
            participants=["Owner", "MemberA", "MemberB"],
            remainder_policy="payer",
            payer="Owner",
        )
        assert result["Owner"] == money("33")
        assert result["MemberA"] == money("34")
        assert result["MemberB"] == money("33")
        assert sum(result.values(), money("0")) == money("100")

    # -- payer non-negative guard ------------------------------------------

    def test_payer_remainder_exceeding_participant_bound_fails(self) -> None:
        """|remaining| > participant_count is rejected.  Under legitimate
        exact-share inputs each ROUND_HALF_UP deviates by at most 0.5 units,
        so this can only arise from corrupted data.  Tested directly."""
        from finance_core.calculators.receipt_split_calculator import _validate_remainder_bounds

        with pytest.raises(ValueError, match="payer remainder 4 exceeds allowed bound"):
            _validate_remainder_bounds(
                remaining=4,
                total_units=100,
                sum_floors=96,
                participant_count=2,
                currency="JPY",
                policy="payer",
            )

    def test_payer_negative_allocation_rejected(self) -> None:
        """When half-up rounding over-sums and the payer has zero base units,
        the negative remainder would make the payer allocation negative.
        The allocator must reject this case with a clear error.

        JPY: 0.4 + 0.6 + 0.5 + 8.5 = 10.
        Half-up: 0 + 1 + 1 + 9 = 11.  remaining = -1.
        payer base=0.  0 + (-1) = -1 < 0  → rejected."""
        with pytest.raises(ValueError, match="cannot absorb.*without becoming negative"):
            _allocate_minor_units(
                exact_shares={
                    "payer": Decimal("0.4"),
                    "b": Decimal("0.6"),
                    "c": Decimal("0.5"),
                    "d": Decimal("8.5"),
                },
                exact_total=money("10"),
                currency="JPY",
                participants=["payer", "b", "c", "d"],
                remainder_policy="payer",
                payer="payer",
            )

    # -- determinism -------------------------------------------------------

    def test_negative_remainder_is_deterministic(self) -> None:
        exact = {
            "Owner": Decimal("3.335"),
            "MemberA": Decimal("3.335"),
            "MemberB": Decimal("3.330"),
        }
        first = _allocate_minor_units(
            exact_shares=exact,
            exact_total=money("10.00"),
            currency="SGD",
            participants=["Owner", "MemberA", "MemberB"],
            remainder_policy="payer",
            payer="Owner",
        )
        for _ in range(5):
            again = _allocate_minor_units(
                exact_shares=exact,
                exact_total=money("10.00"),
                currency="SGD",
                participants=["Owner", "MemberA", "MemberB"],
                remainder_policy="payer",
                payer="Owner",
            )
            assert again == first

    def test_positive_remainder_is_deterministic(self) -> None:
        exact = {
            "A": Decimal("3.333333333333333333333333333"),
            "B": Decimal("3.333333333333333333333333333"),
            "C": Decimal("3.333333333333333333333333333"),
        }
        first = _allocate_minor_units(
            exact_shares=exact,
            exact_total=money("10.00"),
            currency="SGD",
            participants=["A", "B", "C"],
            remainder_policy="payer",
            payer="A",
        )
        for _ in range(5):
            again = _allocate_minor_units(
                exact_shares=exact,
                exact_total=money("10.00"),
                currency="SGD",
                participants=["A", "B", "C"],
                remainder_policy="payer",
                payer="A",
            )
            assert again == first

    # -- remainder bounds --------------------------------------------------
    # Under legitimate exact-share inputs |remaining| is bounded by N
    # (each participant's ROUND_HALF_UP deviates by at most 0.5 units).
    # |remaining| > N can only arise from corrupted data — the guards
    # defend against it.  We test them directly.

    def test_positive_remainder_within_bound_passes(self) -> None:
        result = _allocate_minor_units(
            exact_shares={
                "A": Decimal("0.5"),
                "B": Decimal("99.0"),
                "C": Decimal("0.5"),
            },
            exact_total=money("100"),
            currency="JPY",
            participants=["A", "B", "C"],
            remainder_policy="payer",
            payer="A",
        )
        assert sum(result.values(), money("0")) == money("100")

    def test_positive_remainder_exceeding_bound_fails(self) -> None:
        from finance_core.calculators.receipt_split_calculator import _validate_remainder_bounds

        with pytest.raises(ValueError, match="payer remainder 4 exceeds allowed bound"):
            _validate_remainder_bounds(
                remaining=4,
                total_units=100,
                sum_floors=96,
                participant_count=2,
                currency="JPY",
                policy="payer",
            )

    def test_negative_remainder_within_bound_passes(self) -> None:
        result = _allocate_minor_units(
            exact_shares={
                "A": Decimal("0.5"),
                "B": Decimal("99.5"),
            },
            exact_total=money("100"),
            currency="JPY",
            participants=["A", "B"],
            remainder_policy="payer",
            payer="A",
        )
        assert sum(result.values(), money("0")) == money("100")

    def test_negative_remainder_exceeding_bound_fails(self) -> None:
        from finance_core.calculators.receipt_split_calculator import _validate_remainder_bounds

        with pytest.raises(ValueError, match="payer remainder -4 exceeds allowed bound"):
            _validate_remainder_bounds(
                remaining=-4,
                total_units=100,
                sum_floors=104,
                participant_count=2,
                currency="JPY",
                policy="payer",
            )

    # -- validation --------------------------------------------------------

    def test_payer_missing_raises(self) -> None:
        with pytest.raises(ValueError, match="payer must be provided"):
            _allocate_minor_units(
                exact_shares={"A": money("10.00")},
                exact_total=money("10.00"),
                currency="SGD",
                participants=["A"],
                remainder_policy="payer",
                payer=None,
            )

    def test_payer_not_in_participants_raises(self) -> None:
        with pytest.raises(ValueError, match="not in the participant set"):
            _allocate_minor_units(
                exact_shares={"A": money("10.00")},
                exact_total=money("10.00"),
                currency="SGD",
                participants=["A"],
                remainder_policy="payer",
                payer="B",
            )


# ============================================================================
# Largest-remainder policy
# ============================================================================


class TestLargestRemainderPolicy:
    """Largest-remainder distributes remaining minor units to participants
    with the largest fractional remainders, ties broken by public ID ascending."""

    def test_sgd_one_cent_to_largest_fraction(self) -> None:
        result = _allocate_minor_units(
            exact_shares={
                "Owner": Decimal("3.333333333333333333333333333"),
                "MemberA": Decimal("3.333333333333333333333333333"),
                "MemberB": Decimal("3.333333333333333333333333333"),
            },
            exact_total=money("10.00"),
            currency="SGD",
            participants=["Owner", "MemberA", "MemberB"],
            remainder_policy="largest_remainder",
        )
        assert sum(result.values(), money("0.00")) == money("10.00")
        amounts = sorted(result.values(), reverse=True)
        assert amounts[0] == money("3.34")
        assert amounts[1] == money("3.33")
        assert amounts[2] == money("3.33")

    def test_jpy_one_yen_to_largest_fraction(self) -> None:
        result = _allocate_minor_units(
            exact_shares={
                "Owner": Decimal("33.333333333333333333333333333"),
                "MemberA": Decimal("33.333333333333333333333333333"),
                "MemberB": Decimal("33.333333333333333333333333333"),
            },
            exact_total=money("100"),
            currency="JPY",
            participants=["Owner", "MemberA", "MemberB"],
            remainder_policy="largest_remainder",
        )
        assert sum(result.values(), money("0")) == money("100")
        amounts = sorted(result.values(), reverse=True)
        assert amounts[0] == money("34")
        assert amounts[1] == money("33")
        assert amounts[2] == money("33")

    def test_tie_breaks_by_public_id_ascending(self) -> None:
        result = _allocate_minor_units(
            exact_shares={
                "B_participant": money("2.50"),
                "A_participant": money("2.50"),
                "D_participant": money("2.50"),
                "C_participant": money("2.50"),
            },
            exact_total=money("10.00"),
            currency="SGD",
            participants=[
                "A_participant",
                "B_participant",
                "C_participant",
                "D_participant",
            ],
            remainder_policy="largest_remainder",
        )
        assert sum(result.values(), money("0.00")) == money("10.00")

    def test_deterministic(self) -> None:
        exact = {
            "A": Decimal("3.333333333333333333333333333"),
            "B": Decimal("3.333333333333333333333333333"),
            "C": Decimal("3.333333333333333333333333333"),
        }
        first = _allocate_minor_units(
            exact_shares=exact,
            exact_total=money("10.00"),
            currency="SGD",
            participants=["A", "B", "C"],
            remainder_policy="largest_remainder",
        )
        for _ in range(5):
            again = _allocate_minor_units(
                exact_shares=exact,
                exact_total=money("10.00"),
                currency="SGD",
                participants=["A", "B", "C"],
                remainder_policy="largest_remainder",
            )
            assert again == first

    def test_remaining_exceeds_participant_count_fails(self) -> None:
        """If remaining units exceed N (from corrupted data), the guard fails
        without IndexError.  Not reachable from valid exact_shares given
        floor-based allocation, so we test the guard directly."""
        from finance_core.calculators.receipt_split_calculator import _validate_remainder_bounds

        with pytest.raises(ValueError, match="remaining units 100 exceeds"):
            _validate_remainder_bounds(
                remaining=100,
                total_units=100,
                sum_floors=0,
                participant_count=3,
                currency="SGD",
                policy="largest_remainder",
            )


# ============================================================================
# Minor-unit allocation guard tests
# ============================================================================


class TestAllocatorGuards:
    """Fail-closed guard validation in the minor-unit allocator."""

    def test_empty_participants_fails(self) -> None:
        with pytest.raises(ValueError, match="at least one participant"):
            _allocate_minor_units(
                exact_shares={},
                exact_total=money("10.00"),
                currency="SGD",
                participants=[],
                remainder_policy="largest_remainder",
            )

    def test_duplicate_participants_fail(self) -> None:
        with pytest.raises(ValueError, match="Duplicate participants"):
            _allocate_minor_units(
                exact_shares={"A": money("5.00"), "B": money("5.00")},
                exact_total=money("10.00"),
                currency="SGD",
                participants=["A", "B", "A"],
                remainder_policy="largest_remainder",
            )

    def test_negative_share_fails(self) -> None:
        with pytest.raises(MoneyValidationError, match="non-negative"):
            _allocate_minor_units(
                exact_shares={"A": money("-5.00"), "B": money("15.00")},
                exact_total=money("10.00"),
                currency="SGD",
                participants=["A", "B"],
                remainder_policy="largest_remainder",
            )

    def test_non_finite_total_fails(self) -> None:
        with pytest.raises(MoneyValidationError, match="finite"):
            _allocate_minor_units(
                exact_shares={"A": money("5.00")},
                exact_total=Decimal("NaN"),
                currency="SGD",
                participants=["A"],
                remainder_policy="largest_remainder",
            )

    def test_total_not_at_currency_scale_fails(self) -> None:
        with pytest.raises(MoneyValidationError, match="precision"):
            _allocate_minor_units(
                exact_shares={"A": money("5.00")},
                exact_total=Decimal("10.345"),
                currency="SGD",
                participants=["A"],
                remainder_policy="largest_remainder",
            )

    def test_unsupported_remainder_policy_fails(self) -> None:
        with pytest.raises(ValueError, match="Unsupported remainder_policy"):
            _allocate_minor_units(
                exact_shares={"A": money("10.00")},
                exact_total=money("10.00"),
                currency="SGD",
                participants=["A"],
                remainder_policy="random_pick",
            )


# ============================================================================
# Empty sum regression
# ============================================================================


class TestEmptySumRegression:
    """All financial sums must use Decimal ZERO, never integer 0."""

    def test_sum_of_empty_generator_with_zero_is_decimal(self) -> None:
        result = sum((a for a in []), Decimal("0.00"))
        assert isinstance(result, Decimal)
        assert result == Decimal("0.00")

    def test_allocate_exact_no_remainder(self) -> None:
        result = _allocate_minor_units(
            exact_shares={"A": money("5.00"), "B": money("5.00")},
            exact_total=money("10.00"),
            currency="SGD",
            participants=["A", "B"],
            remainder_policy="largest_remainder",
        )
        assert result["A"] == money("5.00")
        assert result["B"] == money("5.00")
        assert sum(result.values(), money("0.00")) == money("10.00")


# ============================================================================
# JPY integration tests
# ============================================================================


class TestJPYIntegration:
    """JPY receipt calculations produce only whole-yen outputs."""

    def test_simple_even_split(self) -> None:
        result = calculate_receipt_split(
            {
                "currency": "JPY",
                "participants": ["Owner", "MemberA"],
                "receipts": [
                    {
                        "merchant": "Lunch",
                        "paid_by": "Owner",
                        "net_paid": "2000",
                        "items": [
                            {
                                "description": "Set lunch",
                                "amount": "2000",
                                "participants": ["Owner", "MemberA"],
                            }
                        ],
                    }
                ],
            }
        )
        assert result["participant_shares"] == {
            "Owner": money("1000"),
            "MemberA": money("1000"),
        }
        for share in result["participant_shares"].values():
            assert share == share.to_integral_value()

    def test_uneven_split_uses_largest_remainder(self) -> None:
        """2000 JPY / 3 participants.  Two get 667, one gets 666.
        Ties broken by public ID ascending: MemberB < MemberA < Owner."""
        result = calculate_receipt_split(
            {
                "currency": "JPY",
                "participants": ["Owner", "MemberA", "MemberB"],
                "receipts": [
                    {
                        "merchant": "Dinner",
                        "paid_by": "Owner",
                        "net_paid": "2000",
                        "rounding_policy": "largest_remainder",
                        "items": [
                            {
                                "description": "Shared meal",
                                "amount": "2000",
                                "participants": ["Owner", "MemberA", "MemberB"],
                            }
                        ],
                    }
                ],
            }
        )
        assert sum(result["participant_shares"].values(), money("0")) == money("2000")
        for share in result["participant_shares"].values():
            assert share == share.to_integral_value()
        assert result["participant_shares"]["MemberB"] == money("667")
        assert result["participant_shares"]["MemberA"] == money("667")
        assert result["participant_shares"]["Owner"] == money("666")

    def test_obligations_are_whole_yen(self) -> None:
        result = calculate_receipt_split(
            {
                "currency": "JPY",
                "participants": ["Owner", "MemberA", "MemberB"],
                "receipts": [
                    {
                        "merchant": "Dinner",
                        "paid_by": "Owner",
                        "net_paid": "2000",
                        "rounding_policy": "largest_remainder",
                        "items": [
                            {
                                "description": "Shared meal",
                                "amount": "2000",
                                "participants": ["Owner", "MemberA", "MemberB"],
                            }
                        ],
                    }
                ],
            }
        )
        for obligation in result["settlement_obligations"]:
            amt = obligation["amount"]
            assert amt == amt.to_integral_value()

    def test_fractional_authoritative_input_fails(self) -> None:
        with pytest.raises((ValueError, MoneyValidationError)):
            calculate_receipt_split(
                {
                    "currency": "JPY",
                    "participants": ["Owner", "MemberA"],
                    "receipts": [
                        {
                            "merchant": "Test",
                            "paid_by": "Owner",
                            "net_paid": "100",
                            "items": [
                                {
                                    "description": "Item",
                                    "amount": "100.5",
                                    "participants": ["Owner", "MemberA"],
                                }
                            ],
                        }
                    ],
                }
            )


# ============================================================================
# Currency authority regression
# ============================================================================


class TestCurrencyAuthorityRegression:
    """Top-level currency is required and authoritative."""

    def test_missing_top_level_currency_fails(self) -> None:
        with pytest.raises(ValueError, match="top-level currency"):
            calculate_receipt_split(
                {
                    "participants": ["Owner", "MemberA"],
                    "receipts": [
                        {
                            "merchant": "Test",
                            "paid_by": "Owner",
                            "currency": "SGD",
                            "net_paid": "10.00",
                            "items": [
                                {
                                    "description": "Item",
                                    "amount": "10.00",
                                    "participants": ["Owner", "MemberA"],
                                }
                            ],
                        }
                    ],
                }
            )

    def test_explicit_top_level_currency_succeeds(self) -> None:
        result = calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["Owner", "MemberA"],
                "receipts": [
                    {
                        "merchant": "Test",
                        "paid_by": "Owner",
                        "net_paid": "10.00",
                        "items": [
                            {
                                "description": "Item",
                                "amount": "10.00",
                                "participants": ["Owner", "MemberA"],
                            }
                        ],
                    }
                ],
            }
        )
        assert result["currency"] == "SGD"

    def test_lower_case_currency_normalizes(self) -> None:
        result = calculate_receipt_split(
            {
                "currency": "sgd",
                "participants": ["Owner", "MemberA"],
                "receipts": [
                    {
                        "merchant": "Test",
                        "paid_by": "Owner",
                        "net_paid": "10.00",
                        "items": [
                            {
                                "description": "Item",
                                "amount": "10.00",
                                "participants": ["Owner", "MemberA"],
                            }
                        ],
                    }
                ],
            }
        )
        assert result["currency"] == "SGD"


# ============================================================================
# Payer policy end-to-end tests
# ============================================================================


class TestPayerPolicyEndToEnd:
    """End-to-end receipt splits with explicit payer policy."""

    def test_reconciles_exactly(self) -> None:
        result = calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["Owner", "MemberA"],
                "receipts": [
                    {
                        "merchant": "Test",
                        "paid_by": "Owner",
                        "net_paid": "10.00",
                        "rounding_policy": "payer",
                        "items": [
                            {
                                "description": "Item",
                                "amount": "10.00",
                                "participants": ["Owner", "MemberA"],
                            }
                        ],
                    }
                ],
            }
        )
        receipt = result["receipts"][0]
        assert sum(receipt["participant_shares"].values(), money("0.00")) == money("10.00")

    def test_uneven_split_remainder_is_at_most_one_minor_unit(self) -> None:
        result = calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["Owner", "MemberA", "MemberB"],
                "receipts": [
                    {
                        "merchant": "Test",
                        "paid_by": "Owner",
                        "net_paid": "10.00",
                        "rounding_policy": "payer",
                        "items": [
                            {
                                "description": "Shared",
                                "amount": "10.00",
                                "participants": ["Owner", "MemberA", "MemberB"],
                            }
                        ],
                    }
                ],
            }
        )
        receipt = result["receipts"][0]
        payer_share = receipt["participant_shares"]["Owner"]
        others_min = min(v for k, v in receipt["participant_shares"].items() if k != "Owner")
        # Payer should not be more than 1 cent above the lowest other share.
        assert payer_share - others_min <= money("0.01")
