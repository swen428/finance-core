from __future__ import annotations

import sqlite3
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from finance_core.money import money_decimal, quantum_for_currency, require_same_currency

# Keep group eligibility narrow until finalisation defines a broader lifecycle.
# TC001 is already calculated; active groups represent not-yet-settled input.
ELIGIBLE_RECEIPT_GROUP_STATUSES = {"active", "calculated"}
ELIGIBLE_RECEIPT_STATUSES = {"confirmed"}


def load_receipt_group_calculation_input(
    conn: sqlite3.Connection,
    receipt_group_public_id: str,
) -> dict[str, Any]:
    """Load confirmed receipt group facts into receipt split calculator input."""
    group = _get_receipt_group(conn, receipt_group_public_id)
    _require_receipt_group_calculation_status(group)
    participants, participant_display_names = _load_group_participants(
        conn, receipt_group_public_id
    )
    receipts = _load_group_receipts(
        conn,
        receipt_group_public_id,
        group["currency"],
    )
    payers = _distinct_payers(receipts)

    case_data: dict[str, Any] = {
        "case_id": group["public_id"],
        "currency": group["currency"],
        "status": "calculated_pending_confirmation",
        "participants": participants,
        "participant_display_names": participant_display_names,
        "receipts": receipts,
    }
    if len(payers) == 1:
        case_data["payer"] = payers[0]
    return case_data


def _get_receipt_group(
    conn: sqlite3.Connection,
    receipt_group_public_id: str,
) -> dict[str, Any]:
    row = _fetch_one_dict(
        conn,
        """
        SELECT public_id, currency, status
        FROM receipt_groups
        WHERE public_id = ?
        """,
        (receipt_group_public_id,),
    )
    if row is None:
        raise ValueError(f"Receipt group not found: {receipt_group_public_id}")
    return row


def _require_receipt_group_calculation_status(group: dict[str, Any]) -> None:
    status = group["status"]
    if status not in ELIGIBLE_RECEIPT_GROUP_STATUSES:
        raise ValueError(
            f"Receipt group {group['public_id']} has status {status}; "
            f"eligible statuses are {_status_list(ELIGIBLE_RECEIPT_GROUP_STATUSES)}"
        )


def _load_group_participants(
    conn: sqlite3.Connection,
    receipt_group_public_id: str,
) -> tuple[list[str], dict[str, str]]:
    rows = _fetch_all_dicts(
        conn,
        """
        SELECT DISTINCT p.id, p.public_id, p.display_name
        FROM receipt_groups rg
        JOIN receipt_group_receipts rgr ON rgr.receipt_group_id = rg.id
        JOIN receipt_participants rp ON rp.receipt_id = rgr.receipt_id
        JOIN participants p ON p.id = rp.participant_id
        WHERE rg.public_id = ?
          AND rp.is_included = 1
        ORDER BY p.id
        """,
        (receipt_group_public_id,),
    )
    if not rows:
        raise ValueError(f"Receipt group has no included participants: {receipt_group_public_id}")

    participant_public_ids: list[str] = []
    participant_display_names: dict[str, str] = {}

    for row in rows:
        pub_id = row["public_id"]
        if pub_id in participant_public_ids:
            raise ValueError(
                f"Duplicate participant public_id {pub_id!r} in receipt group "
                f"{receipt_group_public_id}"
            )
        participant_public_ids.append(pub_id)
        participant_display_names[pub_id] = row["display_name"]

    return participant_public_ids, participant_display_names


def _load_group_receipts(
    conn: sqlite3.Connection,
    receipt_group_public_id: str,
    group_currency: str,
) -> list[dict[str, Any]]:
    receipt_rows = _fetch_all_dicts(
        conn,
        """
        SELECT
          r.id,
          r.public_id,
          r.merchant,
          r.currency,
          p.public_id AS paid_by,
          r.gross_amount,
          r.subtotal_amount,
          r.service_charge_amount,
          r.discount_amount,
          r.net_paid_amount,
          r.status
        FROM receipt_groups rg
        JOIN receipt_group_receipts rgr ON rgr.receipt_group_id = rg.id
        JOIN receipts r ON r.id = rgr.receipt_id
        JOIN participants p ON p.id = r.payer_participant_id
        WHERE rg.public_id = ?
        ORDER BY rgr.sequence_number, r.id
        """,
        (receipt_group_public_id,),
    )
    if not receipt_rows:
        raise ValueError(f"Receipt group has no receipts: {receipt_group_public_id}")
    receipts = []
    for receipt_row in receipt_rows:
        _require_receipt_calculation_status(receipt_row, receipt_group_public_id)
        require_same_currency(
            group_currency,
            receipt_row["currency"],
            label_a="receipt group currency",
            label_b="receipt currency",
        )
        receipts.append(_load_receipt(conn, receipt_row))
    return receipts


def _require_receipt_calculation_status(
    receipt: dict[str, Any],
    receipt_group_public_id: str,
) -> None:
    status = receipt["status"]
    if status not in ELIGIBLE_RECEIPT_STATUSES:
        raise ValueError(
            f"Receipt {receipt['public_id']} in group {receipt_group_public_id} "
            f"has status {status}; eligible statuses are {_status_list(ELIGIBLE_RECEIPT_STATUSES)}"
        )


def _load_receipt(conn: sqlite3.Connection, receipt_row: dict[str, Any]) -> dict[str, Any]:
    currency = receipt_row["currency"]
    receipt = {
        "receipt_id": receipt_row["public_id"],
        "merchant": receipt_row["merchant"],
        "paid_by": receipt_row["paid_by"],
        "currency": currency,
        "gross_bill": _money_text(receipt_row["gross_amount"], currency),
        "subtotal": _money_text(receipt_row["subtotal_amount"], currency),
        "discount": _money_text(receipt_row["discount_amount"] or 0, currency),
        "net_paid": _money_text(receipt_row["net_paid_amount"], currency),
        "rounding_policy": "payer",
        "items": _load_receipt_items(conn, receipt_row["id"], currency),
    }

    for adjustment in _fetch_all_dicts(
        conn,
        """
        SELECT public_id, currency, adjustment_type, direction, allocation_method
        FROM receipt_adjustments
        WHERE receipt_id = ?
        ORDER BY priority, id
        """,
        (receipt_row["id"],),
    ):
        require_same_currency(
            currency,
            adjustment["currency"],
            label_a="receipt currency",
            label_b="receipt adjustment currency",
        )
        if adjustment["adjustment_type"] == "service_charge":
            receipt["service_charge_amount"] = _money_text(
                receipt_row["service_charge_amount"] or 0,
                currency,
            )
            receipt["service_charge_allocation_method"] = adjustment["allocation_method"]
        elif adjustment["direction"] == "subtract":
            receipt["discount_allocation_method"] = adjustment["allocation_method"]

    return receipt


def _load_receipt_items(
    conn: sqlite3.Connection,
    receipt_id: int,
    currency: str,
) -> list[dict[str, Any]]:
    items = []
    for item_row in _fetch_all_dicts(
        conn,
        """
        SELECT
          id,
          public_id,
          currency,
          item_name,
          quantity,
          unit_price,
          line_amount
        FROM receipt_items
        WHERE receipt_id = ?
        ORDER BY line_number, id
        """,
        (receipt_id,),
    ):
        item_currency = item_row["currency"]
        require_same_currency(
            currency,
            item_currency,
            label_a="receipt currency",
            label_b="receipt item currency",
        )
        allocations = _load_item_allocations(conn, item_row["id"], item_currency)
        item = {
            "description": item_row["item_name"],
            "currency": item_currency,
            "quantity": _number_text(item_row["quantity"]),
            "unit_price": _money_text(item_row["unit_price"], item_currency),
            "amount": _money_text(item_row["line_amount"], item_currency),
        }
        if _requires_manual_item_allocation(allocations):
            item["allocation_method"] = "manual"
            item["allocations"] = {
                allocation["participant"]: allocation["amount"] for allocation in allocations
            }
        else:
            item["participants"] = [allocation["participant"] for allocation in allocations]
        items.append(item)
    if not items:
        raise ValueError(f"Receipt has no items: {receipt_id}")
    return items


def _load_item_allocations(
    conn: sqlite3.Connection,
    receipt_item_id: int,
    currency: str,
) -> list[dict[str, str]]:
    rows = _fetch_all_dicts(
        conn,
        """
        SELECT
          p.public_id AS participant,
          ria.share_amount_before_service_charge AS amount,
          ria.allocation_method
        FROM receipt_item_allocations ria
        JOIN participants p ON p.id = ria.participant_id
        WHERE ria.receipt_item_id = ?
        ORDER BY p.id
        """,
        (receipt_item_id,),
    )
    if not rows:
        raise ValueError(f"Receipt item has no allocations: {receipt_item_id}")
    return [
        {
            "participant": row["participant"],
            "amount": _money_text(row["amount"], currency),
            "allocation_method": row["allocation_method"],
        }
        for row in rows
    ]


def _requires_manual_item_allocation(allocations: list[dict[str, str]]) -> bool:
    if len(allocations) == 1:
        return False
    allocation_methods = {allocation["allocation_method"] for allocation in allocations}
    return not allocation_methods <= {"equal_quantity", "equal_amount"}


def _distinct_payers(receipts: list[dict[str, Any]]) -> list[str]:
    payers = []
    for receipt in receipts:
        payer = receipt["paid_by"]
        if payer not in payers:
            payers.append(payer)
    return payers


def _money_text(value: Any, currency: str) -> str:
    """Convert a DB value to a canonical decimal string.

    SQLite may return numeric columns as Python floats, so the persistence
    boundary must handle float-to-Decimal conversion.  The Money Contract's
    ``money_decimal`` rejects floats at authoritiative intake boundaries;
    this adapter is the one place where DB→Decimal conversion is permitted.
    """
    if value is None:
        raise ValueError("Currency value is required")
    if isinstance(value, float):
        dec = Decimal(str(value))
    else:
        dec = money_decimal(value, label="persisted monetary value")
    return str(dec.quantize(quantum_for_currency(currency), rounding=ROUND_HALF_UP))


def _number_text(value: Any) -> str | None:
    if value is None:
        return None
    dec = money_decimal(value, label="number value")
    return str(dec.normalize())


def _status_list(statuses: set[str]) -> str:
    return ", ".join(sorted(statuses))


def _fetch_one_dict(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...],
) -> dict[str, Any] | None:
    cursor = conn.execute(sql, params)
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(row, _require_description(cursor))


def _fetch_all_dicts(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...],
) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    description = _require_description(cursor)
    return [_row_to_dict(row, description) for row in cursor.fetchall()]


def _require_description(cursor: sqlite3.Cursor) -> tuple[Any, ...]:
    if cursor.description is None:
        raise RuntimeError("SQLite cursor did not expose column metadata for a loader query")
    return cursor.description


def _row_to_dict(
    row: sqlite3.Row | tuple[Any, ...],
    description: tuple[Any, ...],
) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    columns = [column[0] for column in description]
    return dict(zip(columns, row, strict=True))
