from decimal import Decimal

import pytest

from finance_core.calculators.receipt_split_calculator import calculate_receipt_split


def money(value: str) -> Decimal:
    return Decimal(value)


def test_simple_equal_split_between_two_people() -> None:
    result = calculate_receipt_split(
        {
            "currency": "SGD",
            "participants": ["Owner", "MemberA"],
            "receipts": [
                {
                    "merchant": "Lunch",
                    "paid_by": "Owner",
                    "net_paid": "20.00",
                    "items": [
                        {
                            "description": "Set lunch",
                            "amount": "20.00",
                            "participants": ["Owner", "MemberA"],
                        }
                    ],
                }
            ],
        }
    )

    assert result["participant_shares"] == {
        "Owner": money("10.00"),
        "MemberA": money("10.00"),
    }
    assert result["obligations"] == {"MemberA": money("10.00")}
    assert result["total_paid_by_payer"] == money("20.00")


def test_item_level_ownership_allocates_single_owner_item() -> None:
    result = calculate_receipt_split(
        {
            "currency": "SGD",
            "participants": ["Owner", "MemberB"],
            "receipts": [
                {
                    "merchant": "Cafe",
                    "paid_by": "Owner",
                    "net_paid": "10.00",
                    "items": [
                        {
                            "description": "Coffee",
                            "amount": "4.00",
                            "owners": ["Owner"],
                        },
                        {
                            "description": "Cake",
                            "amount": "6.00",
                            "owners": ["MemberB"],
                        },
                    ],
                }
            ],
        }
    )

    assert result["participant_shares"] == {
        "Owner": money("4.00"),
        "MemberB": money("6.00"),
    }
    assert result["obligations"] == {"MemberB": money("6.00")}


def test_proportional_discount_allocation_uses_item_amounts() -> None:
    result = calculate_receipt_split(
        {
            "currency": "SGD",
            "participants": ["Owner", "MemberA"],
            "receipts": [
                {
                    "merchant": "Bakery",
                    "paid_by": "Owner",
                    "discount": "4.00",
                    "discount_allocation_method": "proportional_by_item_amount",
                    "net_paid": "36.00",
                    "items": [
                        {
                            "description": "Tart",
                            "amount": "10.00",
                            "consumers": ["Owner"],
                        },
                        {
                            "description": "Cake",
                            "amount": "30.00",
                            "consumers": ["MemberA"],
                        },
                    ],
                }
            ],
        }
    )

    assert result["participant_shares"] == {
        "Owner": money("9.00"),
        "MemberA": money("27.00"),
    }
    assert result["receipts"][0]["adjustments"][0]["participant_allocations"] == {
        "Owner": money("1.00"),
        "MemberA": money("3.00"),
    }


def test_equal_per_participant_capped_discount_allocation() -> None:
    result = calculate_receipt_split(
        {
            "currency": "SGD",
            "participants": ["Owner", "MemberC"],
            "receipts": [
                {
                    "merchant": "Dinner",
                    "paid_by": "Owner",
                    "discount": "10.00",
                    "discount_allocation_method": "equal_per_participant",
                    "net_paid": "30.00",
                    "items": [
                        {
                            "description": "Main",
                            "amount": "10.00",
                            "participants": ["Owner"],
                        },
                        {
                            "description": "Premium main",
                            "amount": "30.00",
                            "participants": ["MemberC"],
                        },
                    ],
                }
            ],
        }
    )

    assert result["participant_shares"] == {
        "Owner": money("5.00"),
        "MemberC": money("25.00"),
    }
    assert result["receipts"][0]["adjustments"][0]["participant_allocations"] == {
        "Owner": money("5.00"),
        "MemberC": money("5.00"),
    }


def test_manual_discount_allocation_must_match_adjustment_amount() -> None:
    result = calculate_receipt_split(
        {
            "currency": "SGD",
            "participants": ["Owner", "MemberA"],
            "receipts": [
                {
                    "merchant": "Grocer",
                    "paid_by": "Owner",
                    "adjustments": [
                        {
                            "type": "discount",
                            "direction": "subtract",
                            "amount": "3.00",
                            "allocation_method": "manual",
                            "allocations": {
                                "Owner": "1.00",
                                "MemberA": "2.00",
                            },
                        }
                    ],
                    "net_paid": "17.00",
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

    assert result["participant_shares"] == {
        "Owner": money("9.00"),
        "MemberA": money("8.00"),
    }
    assert result["receipts"][0]["adjustments"][0]["participant_allocations"] == {
        "Owner": money("1.00"),
        "MemberA": money("2.00"),
    }


def test_rounding_adjustment_follows_payer_and_preserves_receipt_total() -> None:
    result = calculate_receipt_split(
        {
            "currency": "SGD",
            "participants": ["Owner", "MemberA", "MemberB"],
            "receipts": [
                {
                    "merchant": "Snacks",
                    "paid_by": "Owner",
                    "rounding_policy": "payer",
                    "net_paid": "10.00",
                    "items": [
                        {
                            "description": "Shared snacks",
                            "amount": "10.00",
                            "participants": ["Owner", "MemberA", "MemberB"],
                        }
                    ],
                }
            ],
        }
    )

    receipt_result = result["receipts"][0]
    assert receipt_result["participant_shares"] == {
        "Owner": money("3.34"),
        "MemberA": money("3.33"),
        "MemberB": money("3.33"),
    }
    assert receipt_result["rounding_adjustments"] == [
        {"participant": "Owner", "amount": money("0.01"), "policy": "payer"}
    ]
    assert sum(receipt_result["participant_shares"].values(), money("0.00")) == money("10.00")


def test_multiple_receipts_with_same_payer_accumulate_totals() -> None:
    result = calculate_receipt_split(
        {
            "currency": "SGD",
            "participants": ["Owner", "MemberA", "MemberE"],
            "receipts": [
                {
                    "merchant": "Lunch",
                    "paid_by": "Owner",
                    "net_paid": "12.00",
                    "items": [
                        {
                            "description": "Shared meal",
                            "amount": "12.00",
                            "participants": ["Owner", "MemberA"],
                        }
                    ],
                },
                {
                    "merchant": "Dessert",
                    "paid_by": "Owner",
                    "net_paid": "5.00",
                    "items": [
                        {
                            "description": "Dessert",
                            "amount": "5.00",
                            "participants": ["MemberE"],
                        }
                    ],
                },
            ],
        }
    )

    assert result["participant_shares"] == {
        "Owner": money("6.00"),
        "MemberA": money("6.00"),
        "MemberE": money("5.00"),
    }
    assert result["payer_paid_amounts"] == {
        "Owner": money("17.00"),
        "MemberA": money("0.00"),
        "MemberE": money("0.00"),
    }
    assert result["obligations"] == {
        "MemberA": money("6.00"),
        "MemberE": money("5.00"),
    }


def test_multiple_receipts_with_different_payers_net_to_creditors() -> None:
    result = calculate_receipt_split(
        {
            "currency": "SGD",
            "participants": ["Owner", "MemberA", "MemberB"],
            "receipts": [
                {
                    "merchant": "Lunch",
                    "paid_by": "Owner",
                    "net_paid": "20.00",
                    "items": [
                        {
                            "description": "Lunch",
                            "amount": "20.00",
                            "participants": ["Owner", "MemberA"],
                        }
                    ],
                },
                {
                    "merchant": "Tea",
                    "paid_by": "MemberA",
                    "net_paid": "10.00",
                    "items": [
                        {
                            "description": "Tea",
                            "amount": "10.00",
                            "participants": ["MemberA", "MemberB"],
                        }
                    ],
                },
            ],
        }
    )

    assert result["payer_paid_amounts"] == {
        "Owner": money("20.00"),
        "MemberA": money("10.00"),
        "MemberB": money("0.00"),
    }
    assert result["participant_shares"] == {
        "Owner": money("10.00"),
        "MemberA": money("15.00"),
        "MemberB": money("5.00"),
    }
    assert result["settlement_obligations"] == [
        {
            "debtor": "MemberA",
            "creditor": "Owner",
            "amount": money("5.00"),
            "currency": "SGD",
        },
        {
            "debtor": "MemberB",
            "creditor": "Owner",
            "amount": money("5.00"),
            "currency": "SGD",
        },
    ]
    assert result["total_paid"] == sum(result["participant_shares"].values(), money("0.00"))


def test_money_values_must_not_be_floats() -> None:
    with pytest.raises(TypeError, match="must not be floats"):
        calculate_receipt_split(
            {
                "currency": "SGD",
                "participants": ["Owner", "MemberA"],
                "receipts": [
                    {
                        "merchant": "Lunch",
                        "paid_by": "Owner",
                        "net_paid": "20.00",
                        "items": [
                            {
                                "description": "Set lunch",
                                "amount": 20.00,
                                "participants": ["Owner", "MemberA"],
                            }
                        ],
                    }
                ],
            }
        )
