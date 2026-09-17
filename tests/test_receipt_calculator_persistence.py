import sqlite3
from decimal import Decimal

import pytest

from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
from finance_core.calculators.receipt_split_persistence import load_receipt_group_calculation_input
from finance_core.money import CurrencyMismatchError, MoneyValidationError

TC001_GROUP_PUBLIC_ID = "rg_test_case_001"


def money(value: str) -> Decimal:
    return Decimal(value)


def test_load_receipt_group_calculation_input_matches_tc001(
    tc001_db: sqlite3.Connection,
) -> None:
    before_counts = persistence_boundary_counts(tc001_db)

    case_data = load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)

    assert case_data["case_id"] == TC001_GROUP_PUBLIC_ID
    assert case_data["currency"] == "SGD"
    assert case_data["status"] == "calculated_pending_confirmation"
    assert case_data["participants"] == [
        "person_owner",
        "person_member_a",
        "person_member_b",
        "person_member_c",
        "person_member_d",
        "person_member_e",
    ]
    assert case_data["participant_display_names"] == {
        "person_owner": "Owner",
        "person_member_a": "MemberA",
        "person_member_b": "MemberB",
        "person_member_c": "MemberC",
        "person_member_d": "MemberD",
        "person_member_e": "MemberE",
    }
    assert case_data["payer"] == "person_owner"
    assert [receipt["merchant"] for receipt in case_data["receipts"]] == [
        "Example Restaurant",
        "Example Tea",
    ]
    assert case_data["receipts"][0]["discount_allocation_method"] == "equal_per_participant"
    assert case_data["receipts"][1]["discount_allocation_method"] == "proportional_by_item_amount"
    assert case_data["receipts"][0]["gross_bill"] == "73.26"
    assert case_data["receipts"][0]["items"][0]["unit_price"] == "10.90"
    assert case_data["receipts"][0]["items"][0]["amount"] == "65.40"

    result = calculate_receipt_split(case_data)

    assert result["participant_shares"] == {
        "person_owner": money("13.55"),
        "person_member_a": money("14.91"),
        "person_member_b": money("17.54"),
        "person_member_c": money("8.92"),
        "person_member_d": money("7.60"),
        "person_member_e": money("7.60"),
    }
    assert result["obligations"] == {
        "person_member_a": money("14.91"),
        "person_member_b": money("17.54"),
        "person_member_c": money("8.92"),
        "person_member_d": money("7.60"),
        "person_member_e": money("7.60"),
    }
    assert persistence_boundary_counts(tc001_db) == before_counts


def test_loader_and_calculator_round_trip_jpy_at_zero_minor_unit(
    tc001_db: sqlite3.Connection,
) -> None:
    _convert_tc001_money_to_jpy(tc001_db)

    case_data = load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)

    assert case_data["currency"] == "JPY"
    assert case_data["receipts"][0]["gross_bill"] == "7326"
    assert case_data["receipts"][0]["items"][0]["unit_price"] == "1090"
    assert case_data["receipts"][0]["items"][0]["amount"] == "6540"

    result = calculate_receipt_split(case_data)

    assert result["currency"] == "JPY"
    assert result["total_paid"] == money("7012")
    assert all(
        amount == amount.to_integral_value() for amount in result["participant_shares"].values()
    )


def test_loader_uses_round_half_up_for_sqlite_float_mirrors(
    tc001_db: sqlite3.Connection,
) -> None:
    tc001_db.execute(
        "UPDATE receipt_items SET unit_price = ? WHERE public_id = ?",
        (2.025, "ri_example_tea_chrysanthemum_tea"),
    )
    tc001_db.commit()

    case_data = load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)

    assert case_data["receipts"][1]["items"][2]["unit_price"] == "2.03"


def test_loader_rejects_item_currency_mismatch_before_returning_input(
    tc001_db: sqlite3.Connection,
) -> None:
    before_counts = persistence_boundary_counts(tc001_db)
    tc001_db.execute(
        "UPDATE receipt_items SET currency = 'JPY' WHERE public_id = ?",
        ("ri_example_restaurant_chicken_pot",),
    )
    tc001_db.commit()

    with pytest.raises(CurrencyMismatchError, match="receipt item currency 'JPY'"):
        load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)

    assert persistence_boundary_counts(tc001_db) == before_counts


def test_loader_rejects_receipt_currency_mismatch_before_returning_input(
    tc001_db: sqlite3.Connection,
) -> None:
    before_counts = persistence_boundary_counts(tc001_db)
    tc001_db.execute(
        "UPDATE receipts SET currency = 'JPY' WHERE public_id = ?",
        ("r_test_case_001_example_restaurant",),
    )
    tc001_db.commit()

    with pytest.raises(CurrencyMismatchError, match="receipt currency 'JPY'"):
        load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)

    assert persistence_boundary_counts(tc001_db) == before_counts


def test_loader_rejects_adjustment_currency_mismatch_before_returning_input(
    tc001_db: sqlite3.Connection,
) -> None:
    before_counts = persistence_boundary_counts(tc001_db)
    tc001_db.execute(
        "UPDATE receipt_adjustments SET currency = 'JPY' WHERE public_id = ?",
        ("ra_example_restaurant_service_charge",),
    )
    tc001_db.commit()

    with pytest.raises(CurrencyMismatchError, match="receipt adjustment currency 'JPY'"):
        load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)

    assert persistence_boundary_counts(tc001_db) == before_counts


def test_loader_rejects_unsupported_nested_currency_before_returning_input(
    tc001_db: sqlite3.Connection,
) -> None:
    before_counts = persistence_boundary_counts(tc001_db)
    tc001_db.execute(
        "UPDATE receipt_items SET currency = 'XYZ' WHERE public_id = ?",
        ("ri_example_restaurant_chicken_pot",),
    )
    tc001_db.commit()

    with pytest.raises(MoneyValidationError, match="Unsupported currency 'XYZ'"):
        load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)

    assert persistence_boundary_counts(tc001_db) == before_counts


def test_loader_rejects_ineligible_receipt_group_status(
    tc001_db: sqlite3.Connection,
) -> None:
    tc001_db.execute(
        "UPDATE receipt_groups SET status = ? WHERE public_id = ?",
        ("needs_review", TC001_GROUP_PUBLIC_ID),
    )
    tc001_db.commit()

    with pytest.raises(ValueError, match="Receipt group .* status .*needs_review"):
        load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)


def test_loader_rejects_ineligible_receipt_status(
    tc001_db: sqlite3.Connection,
) -> None:
    receipt_public_id = "r_test_case_001_example_tea"
    tc001_db.execute(
        "UPDATE receipts SET status = ? WHERE public_id = ?",
        ("draft", receipt_public_id),
    )
    tc001_db.commit()

    with pytest.raises(ValueError, match=f"Receipt {receipt_public_id} .* status .*draft"):
        load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)


def test_loader_supports_connections_without_sqlite_row_factory(
    tc001_db: sqlite3.Connection,
) -> None:
    tc001_db.row_factory = None

    case_data = load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)
    result = calculate_receipt_split(case_data)

    assert case_data["participants"] == [
        "person_owner",
        "person_member_a",
        "person_member_b",
        "person_member_c",
        "person_member_d",
        "person_member_e",
    ]
    assert result["total_paid"] == money("70.12")


def test_loader_and_calculator_do_not_mutate_persistence_tables(
    tc001_db: sqlite3.Connection,
) -> None:
    before_counts = persistence_boundary_counts(tc001_db)

    case_data = load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)
    calculate_receipt_split(case_data)

    assert persistence_boundary_counts(tc001_db) == before_counts


def persistence_boundary_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "transactions",
        "receipt_groups",
        "receipt_group_receipts",
        "receipts",
        "receipt_participants",
        "receipt_items",
        "receipt_item_allocations",
        "receipt_adjustments",
        "calculation_runs",
        "calculation_participant_shares",
        "calculation_adjustment_allocations",
        "settlement_obligations",
    ]
    return {
        table: conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
        for table in tables
    }


def _convert_tc001_money_to_jpy(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE receipt_groups SET currency = 'JPY' WHERE public_id = ?",
        (TC001_GROUP_PUBLIC_ID,),
    )
    conn.execute(
        """
        UPDATE receipts
        SET gross_amount = gross_amount * 100,
            subtotal_amount = subtotal_amount * 100,
            service_charge_amount = service_charge_amount * 100,
            discount_amount = discount_amount * 100,
            net_paid_amount = net_paid_amount * 100,
            currency = 'JPY'
        WHERE id IN (
            SELECT receipt_id
            FROM receipt_group_receipts
            WHERE receipt_group_id = (
                SELECT id FROM receipt_groups WHERE public_id = ?
            )
        )
        """,
        (TC001_GROUP_PUBLIC_ID,),
    )
    conn.execute(
        """
        UPDATE receipt_items
        SET unit_price = unit_price * 100,
            line_amount = line_amount * 100,
            currency = 'JPY'
        WHERE receipt_id IN (
            SELECT receipt_id
            FROM receipt_group_receipts
            WHERE receipt_group_id = (
                SELECT id FROM receipt_groups WHERE public_id = ?
            )
        )
        """,
        (TC001_GROUP_PUBLIC_ID,),
    )
    conn.execute(
        """
        UPDATE receipt_item_allocations
        SET share_amount_before_service_charge = share_amount_before_service_charge * 100
        WHERE receipt_item_id IN (
            SELECT id
            FROM receipt_items
            WHERE receipt_id IN (
                SELECT receipt_id
                FROM receipt_group_receipts
                WHERE receipt_group_id = (
                    SELECT id FROM receipt_groups WHERE public_id = ?
                )
            )
        )
        """,
        (TC001_GROUP_PUBLIC_ID,),
    )
    conn.execute(
        """
        UPDATE receipt_adjustments
        SET amount = amount * 100,
            cap_amount = cap_amount * 100,
            currency = 'JPY'
        WHERE receipt_id IN (
            SELECT receipt_id
            FROM receipt_group_receipts
            WHERE receipt_group_id = (
                SELECT id FROM receipt_groups WHERE public_id = ?
            )
        )
        """,
        (TC001_GROUP_PUBLIC_ID,),
    )
    conn.commit()
