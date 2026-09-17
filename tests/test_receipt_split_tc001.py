import sqlite3
from decimal import Decimal

from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
from finance_core.calculators.receipt_split_persistence import load_receipt_group_calculation_input

TC001_GROUP_PUBLIC_ID = "rg_test_case_001"
TC001_CALCULATION_PUBLIC_ID = "calc_test_case_001_v1"

EXPECTED_PARTICIPANT_SHARES = {
    "person_owner": Decimal("13.55"),
    "person_member_a": Decimal("14.91"),
    "person_member_b": Decimal("17.54"),
    "person_member_c": Decimal("8.92"),
    "person_member_d": Decimal("7.60"),
    "person_member_e": Decimal("7.60"),
}
EXPECTED_OBLIGATIONS = {
    "person_member_a": Decimal("14.91"),
    "person_member_b": Decimal("17.54"),
    "person_member_c": Decimal("8.92"),
    "person_member_d": Decimal("7.60"),
    "person_member_e": Decimal("7.60"),
}


def money(value: str) -> Decimal:
    return Decimal(value)


def load_tc001_db_settlements(conn: sqlite3.Connection) -> dict[str, Decimal]:
    """Load persisted settlements keyed by debtor public_id."""
    return {
        row["debtor"]: money(row["amount"])
        for row in conn.execute(
            """
            SELECT debtor.public_id AS debtor, printf('%.2f', so.amount) AS amount
            FROM settlement_obligations so
            JOIN participants debtor ON debtor.id = so.debtor_id
            JOIN participants creditor ON creditor.id = so.creditor_id
            JOIN calculation_runs cr ON cr.id = so.source_calculation_run_id
            WHERE cr.public_id = ?
              AND creditor.public_id = 'person_owner'
            ORDER BY debtor.public_id
            """,
            (TC001_CALCULATION_PUBLIC_ID,),
        )
    }


def load_tc001_db_creditors(conn: sqlite3.Connection) -> set[str]:
    """Return creditor public_ids from persisted settlement obligations."""
    return {
        row["creditor"]
        for row in conn.execute(
            """
            SELECT DISTINCT creditor.public_id AS creditor
            FROM settlement_obligations so
            JOIN participants creditor ON creditor.id = so.creditor_id
            JOIN calculation_runs cr ON cr.id = so.source_calculation_run_id
            WHERE cr.public_id = ?
            ORDER BY creditor.public_id
            """,
            (TC001_CALCULATION_PUBLIC_ID,),
        )
    }


def test_tc001_receipt_split_expected_obligations(tc001_db: sqlite3.Connection) -> None:
    case_data = load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)

    result = calculate_receipt_split(case_data)

    assert result["status"] == "calculated_pending_confirmation"

    assert result["obligations"] == EXPECTED_OBLIGATIONS
    assert load_tc001_db_settlements(tc001_db) == EXPECTED_OBLIGATIONS
    assert load_tc001_db_creditors(tc001_db) == {"person_owner"}

    assert result["payer_own_share"] == money("13.55")
    assert result["total_paid_by_payer"] == money("70.12")
    assert result["total_to_collect"] == money("56.57")

    assert result["total_to_collect"] + result["payer_own_share"] == result["total_paid_by_payer"]
    assert money("56.57") + money("13.55") == money("70.12")


def test_tc001_participant_shares_sum_to_total_paid(tc001_db: sqlite3.Connection) -> None:
    case_data = load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)

    result = calculate_receipt_split(case_data)

    assert result["participant_shares"] == EXPECTED_PARTICIPANT_SHARES
    assert sum(result["participant_shares"].values(), Decimal("0.00")) == money("70.12")


def test_tc001_receipt_shares_sum_to_each_receipt_net_paid(tc001_db: sqlite3.Connection) -> None:
    case_data = load_receipt_group_calculation_input(tc001_db, TC001_GROUP_PUBLIC_ID)

    result = calculate_receipt_split(case_data)

    receipt_totals = {
        receipt["merchant"]: sum(
            receipt["participant_shares"].values(),
            Decimal("0.00"),
        )
        for receipt in result["receipts"]
    }

    assert receipt_totals["Example Restaurant"] == money("46.93")
    assert receipt_totals["Example Tea"] == money("23.19")
