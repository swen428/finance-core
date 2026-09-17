"""Tests for Settlement Obligation Runtime v1.

Covers: equal split, multiple creditors, multiple debtors, exact settlement,
Decimal handling, zero-obligation suppression, deterministic ordering,
invalid inputs, convenience function, and large-scale deterministic
coverage.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from finance_core.money import quantize_for_currency
from finance_core.settlement.runtime import (
    SettlementObligation,
    SettlementRuntime,
    generate_obligations,
)


def _round_money(value: Decimal) -> Decimal:
    """Round to 2 decimal places for test assertions — SGD-compatible."""
    return quantize_for_currency(value, "SGD")


ZERO = Decimal("0.00")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _obligation_tuple(
    ob: SettlementObligation,
) -> tuple[str, str, Decimal, str, str, str]:
    return (ob.creditor_id, ob.debtor_id, ob.amount, ob.currency, ob.source_type, ob.source_id)


# ---------------------------------------------------------------------------
# Model-level validation
# ---------------------------------------------------------------------------


class TestSettlementObligationValidation:
    def test_valid_obligation(self) -> None:
        ob = SettlementObligation(
            obligation_id="ob_001",
            creditor_id="Owner",
            debtor_id="MemberA",
            amount=Decimal("30.00"),
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_001",
        )
        assert ob.obligation_id == "ob_001"
        assert ob.creditor_id == "Owner"
        assert ob.debtor_id == "MemberA"
        assert ob.amount == Decimal("30.00")
        assert ob.currency == "SGD"

    def test_self_obligation_rejected(self) -> None:
        with pytest.raises(ValueError, match="Self-obligation"):
            SettlementObligation(
                obligation_id="ob_001",
                creditor_id="Owner",
                debtor_id="Owner",
                amount=Decimal("30.00"),
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_zero_amount_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            SettlementObligation(
                obligation_id="ob_001",
                creditor_id="Owner",
                debtor_id="MemberA",
                amount=ZERO,
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_negative_amount_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            SettlementObligation(
                obligation_id="ob_001",
                creditor_id="Owner",
                debtor_id="MemberA",
                amount=Decimal("-10.00"),
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_float_amount_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be Decimal"):
            SettlementObligation(
                obligation_id="ob_001",
                creditor_id="Owner",
                debtor_id="MemberA",
                amount=30.0,  # type: ignore[arg-type]
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_empty_obligation_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="obligation_id is required"):
            SettlementObligation(
                obligation_id="",
                creditor_id="Owner",
                debtor_id="MemberA",
                amount=Decimal("30.00"),
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_empty_creditor_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="creditor_id is required"):
            SettlementObligation(
                obligation_id="ob_001",
                creditor_id="",
                debtor_id="MemberA",
                amount=Decimal("30.00"),
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_empty_debtor_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="debtor_id is required"):
            SettlementObligation(
                obligation_id="ob_001",
                creditor_id="Owner",
                debtor_id="",
                amount=Decimal("30.00"),
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_frozen_dataclass(self) -> None:
        ob = SettlementObligation(
            obligation_id="ob_001",
            creditor_id="Owner",
            debtor_id="MemberA",
            amount=Decimal("30.00"),
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_001",
        )
        with pytest.raises(Exception):
            ob.amount = Decimal("50.00")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SettlementRuntime input validation
# ---------------------------------------------------------------------------


class TestSettlementRuntimeValidation:
    def test_empty_participants_rejected(self) -> None:
        runtime = SettlementRuntime()
        with pytest.raises(ValueError, match="non-empty list"):
            runtime.generate_obligations(
                participants=[],
                balances={},
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_duplicate_participants_rejected(self) -> None:
        runtime = SettlementRuntime()
        with pytest.raises(ValueError, match="Duplicate participants"):
            runtime.generate_obligations(
                participants=["A", "B", "A"],
                balances={"A": ZERO, "B": ZERO},
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_participant_missing_from_balances(self) -> None:
        runtime = SettlementRuntime()
        with pytest.raises(ValueError, match="missing from balances"):
            runtime.generate_obligations(
                participants=["A", "B"],
                balances={"A": ZERO},
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_balance_key_not_in_participants(self) -> None:
        runtime = SettlementRuntime()
        with pytest.raises(ValueError, match="not in participants"):
            runtime.generate_obligations(
                participants=["A", "B"],
                balances={"A": ZERO, "B": ZERO, "C": ZERO},
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_balance_not_zero_sum(self) -> None:
        runtime = SettlementRuntime()
        with pytest.raises(ValueError, match="Balances must sum to zero"):
            runtime.generate_obligations(
                participants=["A", "B"],
                balances={"A": Decimal("50"), "B": Decimal("-40")},
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_float_balance_rejected(self) -> None:
        runtime = SettlementRuntime()
        with pytest.raises(ValueError, match="is float"):
            runtime.generate_obligations(
                participants=["A", "B"],
                balances={"A": Decimal("30"), "B": float(-30)},  # type: ignore[dict-item]
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_non_decimal_balance_rejected(self) -> None:
        runtime = SettlementRuntime()
        with pytest.raises(ValueError, match="must be Decimal"):
            runtime.generate_obligations(
                participants=["A", "B"],
                balances={"A": Decimal("30"), "B": -30},  # type: ignore[dict-item]
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_fractional_cent_balance_rejected(self) -> None:
        runtime = SettlementRuntime()
        with pytest.raises(ValueError, match="precision"):
            runtime.generate_obligations(
                participants=["A", "B"],
                balances={"A": Decimal("0.015"), "B": Decimal("-0.015")},
                currency="SGD",
                source_type="receipt_split",
                source_id="calc_fractional_cent",
            )

    def test_empty_currency_rejected(self) -> None:
        runtime = SettlementRuntime()
        with pytest.raises(ValueError, match="currency is required"):
            runtime.generate_obligations(
                participants=["A", "B"],
                balances={"A": ZERO, "B": ZERO},
                currency="",
                source_type="receipt_split",
                source_id="calc_001",
            )

    def test_empty_source_type_rejected(self) -> None:
        runtime = SettlementRuntime()
        with pytest.raises(ValueError, match="source_type is required"):
            runtime.generate_obligations(
                participants=["A", "B"],
                balances={"A": ZERO, "B": ZERO},
                currency="SGD",
                source_type="",
                source_id="calc_001",
            )

    def test_empty_source_id_rejected(self) -> None:
        runtime = SettlementRuntime()
        with pytest.raises(ValueError, match="source_id is required"):
            runtime.generate_obligations(
                participants=["A", "B"],
                balances={"A": ZERO, "B": ZERO},
                currency="SGD",
                source_type="receipt_split",
                source_id="",
            )


# ---------------------------------------------------------------------------
# Settlement generation -- expected outputs
# ---------------------------------------------------------------------------


class TestSettlementGeneration:
    def test_equal_split_single_creditor(self) -> None:
        runtime = SettlementRuntime()
        obligations = runtime.generate_obligations(
            participants=["A", "B", "C"],
            balances={
                "A": Decimal("60"),
                "B": Decimal("-30"),
                "C": Decimal("-30"),
            },
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_001",
        )
        assert len(obligations) == 2
        tuples = [_obligation_tuple(o) for o in obligations]
        assert ("A", "B", Decimal("30.00"), "SGD", "receipt_split", "calc_001") in tuples
        assert ("A", "C", Decimal("30.00"), "SGD", "receipt_split", "calc_001") in tuples
        total_to_A = sum(o.amount for o in obligations if o.creditor_id == "A")
        assert total_to_A == Decimal("60.00")

    def test_multiple_creditors(self) -> None:
        runtime = SettlementRuntime()
        obligations = runtime.generate_obligations(
            participants=["A", "B", "C"],
            balances={
                "A": Decimal("20"),
                "B": Decimal("10"),
                "C": Decimal("-30"),
            },
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_002",
        )
        assert len(obligations) == 2
        tuples = [_obligation_tuple(o) for o in obligations]
        assert ("A", "C", Decimal("20.00"), "SGD", "receipt_split", "calc_002") in tuples
        assert ("B", "C", Decimal("10.00"), "SGD", "receipt_split", "calc_002") in tuples

    def test_exact_settlement(self) -> None:
        runtime = SettlementRuntime()
        obligations = runtime.generate_obligations(
            participants=["A", "B", "C"],
            balances={"A": ZERO, "B": ZERO, "C": ZERO},
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_003",
        )
        assert len(obligations) == 0

    def test_multiple_debtors(self) -> None:
        runtime = SettlementRuntime()
        participants = ["A", "B", "C", "D"]
        balances = {
            "A": Decimal("90"),
            "B": Decimal("-30"),
            "C": Decimal("-30"),
            "D": Decimal("-30"),
        }
        obligations = runtime.generate_obligations(
            participants=participants,
            balances=balances,
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_004",
        )
        assert len(obligations) == 3
        for debtor in ["B", "C", "D"]:
            found = [o for o in obligations if o.debtor_id == debtor]
            assert len(found) == 1
            assert found[0].creditor_id == "A"
            assert found[0].amount == Decimal("30.00")

    def test_multiple_creditors_multiple_debtors(self) -> None:
        runtime = SettlementRuntime()
        obligations = runtime.generate_obligations(
            participants=["A", "B", "C", "D"],
            balances={
                "A": Decimal("40"),
                "B": ZERO,
                "C": Decimal("-20"),
                "D": Decimal("-20"),
            },
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_005",
        )
        assert len(obligations) == 2
        assert _obligation_tuple(obligations[0])[0] == "A"
        assert _obligation_tuple(obligations[1])[0] == "A"
        amounts = [o.amount for o in obligations]
        assert sum(amounts) == Decimal("40.00")

    def test_zero_obligation_suppression(self) -> None:
        runtime = SettlementRuntime()
        obligations = runtime.generate_obligations(
            participants=["A", "B", "C", "D"],
            balances={
                "A": Decimal("30"),
                "B": Decimal("-30"),
                "C": ZERO,
                "D": ZERO,
            },
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_006",
        )
        assert len(obligations) == 1
        assert obligations[0].creditor_id == "A"
        assert obligations[0].debtor_id == "B"

    def test_obligations_sum_to_creditor_total(self) -> None:
        runtime = SettlementRuntime()
        obligations = runtime.generate_obligations(
            participants=["A", "B", "C", "D"],
            balances={
                "A": Decimal("50"),
                "B": Decimal("10"),
                "C": Decimal("-30"),
                "D": Decimal("-30"),
            },
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_007",
        )
        for creditor in ["A", "B"]:
            total = sum(o.amount for o in obligations if o.creditor_id == creditor)
            expected = {"A": Decimal("50"), "B": Decimal("10")}[creditor]
            assert total == expected
        for debtor in ["C", "D"]:
            total = sum(o.amount for o in obligations if o.debtor_id == debtor)
            expected = {"C": Decimal("30"), "D": Decimal("30")}[debtor]
            assert total == expected


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------


class TestDeterministicOrdering:
    def test_output_order_stable_across_calls(self) -> None:
        runtime = SettlementRuntime()
        args = {
            "participants": ["A", "B", "C"],
            "balances": {"A": Decimal("60"), "B": Decimal("-30"), "C": Decimal("-30")},
            "currency": "SGD",
            "source_type": "receipt_split",
            "source_id": "calc_001",
        }
        results = [runtime.generate_obligations(**args) for _ in range(5)]
        first = [_obligation_tuple(o) for o in results[0]]
        for i, result in enumerate(results[1:], 1):
            assert [_obligation_tuple(o) for o in result] == first, f"Run {i} differs"

    def test_output_order_with_different_participant_names(self) -> None:
        runtime = SettlementRuntime()
        obligations = runtime.generate_obligations(
            participants=["Owner", "MemberA", "MemberB"],
            balances={
                "Owner": Decimal("60"),
                "MemberA": Decimal("-30"),
                "MemberB": Decimal("-30"),
            },
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_008",
        )
        assert obligations[0].creditor_id == "Owner"
        assert obligations[1].creditor_id == "Owner"
        assert obligations[0].debtor_id < obligations[1].debtor_id

    def test_deterministic_ids(self) -> None:
        runtime = SettlementRuntime()
        args = {
            "participants": ["A", "B", "C"],
            "balances": {"A": Decimal("60"), "B": Decimal("-30"), "C": Decimal("-30")},
            "currency": "SGD",
            "source_type": "receipt_split",
            "source_id": "calc_009",
        }
        ob1 = runtime.generate_obligations(**args)
        ob2 = runtime.generate_obligations(**args)
        assert len(ob1) == len(ob2)
        for o1, o2 in zip(ob1, ob2):
            assert o1.obligation_id == o2.obligation_id


# ---------------------------------------------------------------------------
# Decimal handling
# ---------------------------------------------------------------------------


class TestDecimalHandling:
    def test_cents_rounding(self) -> None:
        runtime = SettlementRuntime()
        obligations = runtime.generate_obligations(
            participants=["A", "B", "C"],
            balances={
                "A": Decimal("66.67"),
                "B": Decimal("-33.33"),
                "C": Decimal("-33.34"),
            },
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_010",
        )
        total = sum(o.amount for o in obligations)
        assert total == Decimal("66.67")
        for ob in obligations:
            assert ob.amount == _round_money(ob.amount)

    def test_no_float_contamination(self) -> None:
        runtime = SettlementRuntime()
        obligations = runtime.generate_obligations(
            participants=["A", "B"],
            balances={"A": Decimal("30.00"), "B": Decimal("-30.00")},
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_011",
        )
        for ob in obligations:
            assert isinstance(ob.amount, Decimal)

    def test_small_amounts(self) -> None:
        runtime = SettlementRuntime()
        obligations = runtime.generate_obligations(
            participants=["A", "B"],
            balances={"A": Decimal("0.01"), "B": Decimal("-0.01")},
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_012",
        )
        assert len(obligations) == 1
        assert obligations[0].amount == Decimal("0.01")


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


class TestConvenienceFunction:
    def test_convenience_matches_runtime(self) -> None:
        args = {
            "participants": ["A", "B", "C"],
            "balances": {"A": Decimal("60"), "B": Decimal("-30"), "C": Decimal("-30")},
            "currency": "SGD",
            "source_type": "receipt_split",
            "source_id": "calc_013",
        }
        runtime = SettlementRuntime()
        direct = runtime.generate_obligations(**args)
        conv = generate_obligations(**args)
        assert [_obligation_tuple(o) for o in direct] == [_obligation_tuple(o) for o in conv]

    def test_convenience_defaults(self) -> None:
        obligations = generate_obligations(
            participants=["A", "B"],
            balances={"A": Decimal("30"), "B": Decimal("-30")},
        )
        assert len(obligations) == 1
        assert obligations[0].currency == "SGD"
        assert obligations[0].source_type == "settlement_runtime"
        assert obligations[0].source_id == "default"


# ---------------------------------------------------------------------------
# Large-scale determinism
# ---------------------------------------------------------------------------


class TestLargeScaleDeterminism:
    def test_many_participants(self) -> None:
        runtime = SettlementRuntime()
        balances: dict[str, Decimal] = {}
        participants: list[str] = []
        for i in range(2):
            pid = f"creditor_{i}"
            participants.append(pid)
            balances[pid] = Decimal("90.00")
        total_debt = Decimal("180.00")
        per_debtor = total_debt / 18
        for i in range(18):
            pid = f"debtor_{i}"
            participants.append(pid)
            balances[pid] = -_round_money(per_debtor)
        current_sum = sum(balances.values(), ZERO)
        adjustment = -current_sum
        balances["debtor_17"] += adjustment
        obligations = runtime.generate_obligations(
            participants=participants,
            balances=balances,
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_large",
        )
        total_obligations = sum(o.amount for o in obligations)
        assert total_obligations == Decimal("180.00")
        obligations2 = runtime.generate_obligations(
            participants=participants,
            balances=balances,
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_large",
        )
        assert len(obligations) == len(obligations2)
        for o1, o2 in zip(obligations, obligations2):
            assert o1 == o2
