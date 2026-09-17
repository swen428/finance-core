from datetime import UTC, datetime
from decimal import Decimal

from finance_core.intake.raw_text_logger import log_raw_text_input

FIXED_RECEIVED_AT = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)


def parse(raw_input: str) -> dict:
    return log_raw_text_input(
        raw_input,
        received_at=FIXED_RECEIVED_AT,
        intake_id="test-intake-001",
    )


def test_simple_personal_expense_preserves_raw_input_and_creates_proposal() -> None:
    result = parse("NTUC SGD 28.60 groceries")

    intake = result["intake"]
    proposal = result["proposal"]

    assert intake["raw_input"] == "NTUC SGD 28.60 groceries"
    assert intake["source_type"] == "telegram_text"
    assert intake["status"] == "parsed_pending_confirmation"
    assert proposal["amount"] == Decimal("28.60")
    assert proposal["currency"] == "SGD"
    assert proposal["merchant"] == "NTUC"
    assert "groceries" in proposal["description"]
    assert proposal["category"] == "groceries"
    assert proposal["confirmation_required"] is True
    assert proposal["status"] != "confirmed"
    assert proposal["is_final"] is False
    assert proposal["confidence_metadata"]["overall_score"] == proposal["confidence"]
    assert proposal["field_confidence"]["amount"] > Decimal("0")
    assert any(
        evidence["field_name"] == "amount" and evidence["substring"] == "SGD 28.60"
        for evidence in proposal["field_evidence"]
    )


def test_paid_by_owner_detects_payer_and_merchant() -> None:
    result = parse("Lunch SGD 12.50 at ExampleCafe paid by Owner")

    proposal = result["proposal"]

    assert proposal["amount"] == Decimal("12.50")
    assert proposal["currency"] == "SGD"
    assert proposal["merchant"] == "ExampleCafe"
    assert proposal["paid_by"] == "Owner"
    assert proposal["confirmation_required"] is True
    assert proposal["status"] == "parsed_pending_confirmation"


def test_shared_expense_detects_self_payer_participant_and_equal_split() -> None:
    result = parse("I paid SGD 9.00 for stationery, shared equally with B")

    proposal = result["proposal"]

    assert proposal["amount"] == Decimal("9.00")
    assert proposal["currency"] == "SGD"
    assert proposal["paid_by"] == "Owner"
    assert "B" in proposal["participants"]
    assert proposal["split_type"] == "equal"
    assert proposal["transaction_type"] == "shared_expense"
    assert proposal["intent"] == "shared_expense_log"
    assert proposal["confirmation_required"] is True
    assert proposal["status"] != "confirmed"


def test_missing_amount_stays_pending_and_records_missing_field() -> None:
    result = parse("Coffee at Starbucks")

    proposal = result["proposal"]

    assert "amount" in proposal["missing_fields"]
    assert proposal["confirmation_required"] is True
    assert proposal["status"] == "parsed_pending_confirmation"
    assert proposal["status"] != "confirmed"
    assert proposal["is_final"] is False


def test_raw_input_preserves_mixed_spacing_and_capitalization() -> None:
    raw_input = "  NTUC   SGD 28.60   groceries  "

    result = parse(raw_input)

    assert result["intake"]["raw_input"] == raw_input
    assert result["proposal"]["amount"] == Decimal("28.60")
    assert result["proposal"]["currency"] == "SGD"
    assert result["proposal"]["merchant"] == "NTUC"
    assert result["proposal"]["description"] == "groceries"


def test_source_tracking_preserves_manual_text_reference() -> None:
    result = log_raw_text_input(
        "Starbucks 6.40",
        received_at=FIXED_RECEIVED_AT,
        source_type="manual_text",
        intake_id="manual-intake-001",
    )

    proposal = result["proposal"]

    assert result["intake"]["source_type"] == "manual_text"
    assert proposal["raw_input_reference"] == "manual-intake-001"
    assert proposal["source_tracking"]["source_type"] == "manual_text"
    assert proposal["source_tracking"]["source_reference"] == "manual-intake-001"
    assert proposal["source_tracking"]["raw_input_preserved"] is True


def test_realistic_text_patterns_remain_parser_proposals() -> None:
    cases = [
        (
            "Coffee SGD 6.40 at Starbucks",
            {
                "amount": Decimal("6.40"),
                "currency": "SGD",
                "merchant": "Starbucks",
                "description": "Coffee",
                "transaction_type": "personal_expense",
            },
        ),
        (
            "Starbucks 6.40",
            {
                "amount": Decimal("6.40"),
                "currency": None,
                "merchant": "Starbucks",
                "missing_field": "currency",
            },
        ),
        (
            "Paid 12.50 for lunch",
            {
                "amount": Decimal("12.50"),
                "currency": None,
                "description": "lunch",
                "paid_by": "Owner",
                "transaction_type": "personal_expense",
            },
        ),
        (
            "Lunch $12.50 with B split equally",
            {
                "amount": Decimal("12.50"),
                "currency": "SGD",
                "description": "Lunch",
                "participant": "B",
                "split_type": "equal",
                "transaction_type": "shared_expense",
            },
        ),
        (
            "I paid 30 for B and C, split equally",
            {
                "amount": Decimal("30"),
                "currency": None,
                "participant": "C",
                "paid_by": "Owner",
                "split_type": "equal",
            },
        ),
        (
            "Grab 18.80, B paid me back 9.40",
            {
                "amount": Decimal("18.80"),
                "currency": None,
                "merchant": "Grab",
                "participant": "B",
                "split_type": "unspecified",
            },
        ),
        (
            "NTUC groceries 28.60",
            {
                "amount": Decimal("28.60"),
                "currency": None,
                "merchant": "NTUC",
                "description": "groceries",
            },
        ),
        (
            "JPY 48000 hotel, SGD 430 charged",
            {
                "amount": Decimal("430"),
                "currency": "SGD",
                "description": "hotel",
                "foreign_amount": Decimal("48000"),
                "foreign_currency": "JPY",
            },
        ),
        (
            "Malaysia dinner RM 45.90",
            {
                "amount": Decimal("45.90"),
                "currency": "MYR",
                "description": "Malaysia dinner",
            },
        ),
    ]

    for raw_input, expected in cases:
        proposal = parse(raw_input)["proposal"]

        assert proposal["amount"] == expected["amount"]
        assert proposal["currency"] == expected["currency"]
        assert proposal["confirmation_required"] is True
        assert proposal["status"] == "parsed_pending_confirmation"
        assert proposal["is_final"] is False
        assert proposal["confidence"] >= Decimal("0.10")
        assert proposal["field_confidence"]["amount"] > Decimal("0")
        assert any(evidence["field_name"] == "amount" for evidence in proposal["field_evidence"])
        if "merchant" in expected:
            assert proposal["merchant"] == expected["merchant"]
        if "description" in expected:
            assert proposal["description"] == expected["description"]
        if "transaction_type" in expected:
            assert proposal["transaction_type"] == expected["transaction_type"]
        if "paid_by" in expected:
            assert proposal["paid_by"] == expected["paid_by"]
        if "participant" in expected:
            assert expected["participant"] in proposal["participants"]
        if "split_type" in expected:
            assert proposal["split_type"] == expected["split_type"]
        if "missing_field" in expected:
            assert expected["missing_field"] in proposal["missing_fields"]
        if "foreign_amount" in expected:
            assert proposal["foreign_amount"] == expected["foreign_amount"]
            assert proposal["foreign_currency"] == expected["foreign_currency"]


def test_multiple_amounts_lower_confidence_and_record_secondary_evidence() -> None:
    simple = parse("Coffee SGD 6.40 at Starbucks")["proposal"]
    ambiguous = parse("Grab 18.80, B paid me back 9.40")["proposal"]

    assert ambiguous["confidence"] < simple["confidence"]
    assert ambiguous["secondary_amounts"][0]["amount"] == Decimal("9.40")
    assert any(
        evidence["field_name"] == "secondary_amounts" for evidence in ambiguous["field_evidence"]
    )
