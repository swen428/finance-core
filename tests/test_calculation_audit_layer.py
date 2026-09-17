"""Tests for calculation audit layer v1.

Covers snapshot creation, Decimal safety, settlement obligation reconciliation,
rounding recording, required field guards, float rejection, timestamp injection,
and non-mutation of the original calculator result.
"""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from finance_core.calculation_audit import (
    AuditSnapshot,
    AuditValidationError,
    create_audit_snapshot,
    to_decimal_safe,
    validate_audit_snapshot,
)
from finance_core.calculators.receipt_split_calculator import calculate_receipt_split
from finance_core.money import MoneyValidationError

TC001_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "tc001_receipt_split_example_restaurant_example_tea.json"
)

TEST_AUDIT_CALC_RUN_ID = "calc_audit_test_v1"
TEST_SOURCE_REF = "rg_test_finalization_v1"
FROZEN_NOW = "2026-06-05T12:00:00+08:00"


# -- helpers --


def _tc001_calc_result() -> dict:
    """Return the TC001 calculator result dict."""
    case_data = json.loads(TC001_FIXTURE_PATH.read_text(encoding="utf-8"))
    return calculate_receipt_split(case_data)


def _tc001_frozen_snapshot(**overrides: object) -> AuditSnapshot:
    calc_result = _tc001_calc_result()
    kwargs: dict[str, object] = {
        "calc_result": calc_result,
        "calculation_run_id": TEST_AUDIT_CALC_RUN_ID,
        "source_reference": TEST_SOURCE_REF,
        "now": FROZEN_NOW,
    }
    kwargs.update(overrides)
    return create_audit_snapshot(**kwargs)  # type: ignore[arg-type]


def _tc001_settlement_obligations() -> list[dict]:
    return [dict(o) for o in _tc001_calc_result()["settlement_obligations"]]


def _jpy_calc_result() -> dict:
    return calculate_receipt_split(
        {
            "case_id": "JPY_audit_round_trip",
            "currency": "JPY",
            "participants": ["payer", "friend"],
            "payer": "payer",
            "receipts": [
                {
                    "merchant": "JPY Test Merchant",
                    "paid_by": "payer",
                    "currency": "JPY",
                    "subtotal": "1200",
                    "gross_bill": "1200",
                    "net_paid": "1200",
                    "rounding_policy": "payer",
                    "items": [
                        {
                            "description": "Shared JPY item",
                            "amount": "1200",
                            "participants": ["payer", "friend"],
                        }
                    ],
                }
            ],
        }
    )


# -- 1. Happy path --


def test_audit_snapshot_generation_from_tc001() -> None:
    calc_result = _tc001_calc_result()
    snapshot = create_audit_snapshot(
        calc_result,
        calculation_run_id=TEST_AUDIT_CALC_RUN_ID,
        source_reference=TEST_SOURCE_REF,
        now=FROZEN_NOW,
    )

    assert isinstance(snapshot, AuditSnapshot)
    assert snapshot.calculation_run_id == TEST_AUDIT_CALC_RUN_ID
    assert snapshot.source_type == "receipt_finalization"
    assert snapshot.source_reference == TEST_SOURCE_REF
    assert snapshot.status == "calculated"
    assert snapshot.currency == "SGD"
    assert snapshot.version == 1
    assert snapshot.created_at == FROZEN_NOW

    # Output snapshot
    output = snapshot.output_snapshot
    assert output["participant_shares"]["person_owner"] == "13.55"
    assert output["payer_own_share"] == "13.55"
    assert output["total_paid_by_payer"] == "70.12"
    assert output["total_to_collect"] == "56.57"

    # Settlement snapshot
    settlement = snapshot.settlement_snapshot
    assert settlement["payer"] == "person_owner"
    assert settlement["currency"] == "SGD"
    assert settlement["obligation_count"] == 5
    assert settlement["total_to_collect"] == "56.57"

    # Rounding snapshot -- TC001 has rounding adjustments
    rounding = snapshot.rounding_snapshot
    assert len(rounding) >= 1
    for entry in rounding:
        assert "participant" in entry
        assert "amount" in entry
        assert "policy" in entry


def test_audit_snapshot_serializes_jpy_at_zero_minor_unit() -> None:
    snapshot = create_audit_snapshot(
        _jpy_calc_result(),
        calculation_run_id="calc_audit_jpy_v1",
        source_reference="rg_audit_jpy_v1",
        now=FROZEN_NOW,
    )

    assert snapshot.currency == "JPY"
    assert snapshot.rules_snapshot["rounding_quantum"] == "1"
    assert snapshot.output_snapshot["participant_shares"] == {
        "payer": "600",
        "friend": "600",
    }
    assert snapshot.output_snapshot["total_paid"] == "1200"
    assert snapshot.settlement_snapshot["total_to_collect"] == "600"
    assert snapshot.settlement_snapshot["obligations"][0]["amount"] == "600"


def test_audit_snapshot_serializes_jpy_rounding_adjustment_as_whole_yen() -> None:
    calc_result = calculate_receipt_split(
        {
            "case_id": "JPY_audit_rounding",
            "currency": "JPY",
            "participants": ["payer", "friend_a", "friend_b"],
            "payer": "payer",
            "receipts": [
                {
                    "merchant": "JPY Rounding Merchant",
                    "paid_by": "payer",
                    "currency": "JPY",
                    "net_paid": "100",
                    "rounding_policy": "payer",
                    "items": [
                        {
                            "description": "Uneven JPY item",
                            "amount": "100",
                            "participants": ["payer", "friend_a", "friend_b"],
                        }
                    ],
                }
            ],
        }
    )

    snapshot = create_audit_snapshot(
        calc_result,
        calculation_run_id="calc_audit_jpy_rounding_v1",
        source_reference="rg_audit_jpy_rounding_v1",
        now=FROZEN_NOW,
    )

    assert snapshot.rounding_snapshot == [
        {
            "receipt_label": "JPY Rounding Merchant",
            "participant": "payer",
            "amount": "1",
            "policy": "payer",
        }
    ]


def test_audit_snapshot_uses_round_half_up_for_rounding_evidence() -> None:
    calc_result = _tc001_calc_result()
    calc_result["receipts"][0]["rounding_adjustment"] = Decimal("2.025")
    calc_result["receipts"][0]["rounding_adjustment_participant"] = "person_owner"

    snapshot = create_audit_snapshot(
        calc_result,
        calculation_run_id="calc_audit_half_up_v1",
        source_reference="rg_audit_half_up_v1",
        now=FROZEN_NOW,
    )

    assert snapshot.rounding_snapshot[0]["amount"] == "2.03"


def test_create_audit_snapshot_preserves_unsupported_currency_error_contract() -> None:
    calc_result = _tc001_calc_result()
    calc_result["currency"] = "XYZ"

    with pytest.raises(AuditValidationError, match="Unsupported audit currency 'XYZ'"):
        create_audit_snapshot(
            calc_result,
            calculation_run_id="calc_audit_bad_currency_v1",
            source_reference="rg_audit_bad_currency_v1",
            now=FROZEN_NOW,
        )


@pytest.mark.parametrize("receipt_currency", ["JPY", "XYZ", None])
def test_create_audit_snapshot_rejects_invalid_receipt_currency(
    receipt_currency: object,
) -> None:
    calc_result = _tc001_calc_result()
    calc_result["receipts"][0]["currency"] = receipt_currency

    with pytest.raises(AuditValidationError, match=r"receipt\[0\] currency"):
        create_audit_snapshot(
            calc_result,
            calculation_run_id="calc_audit_bad_receipt_currency_v1",
            source_reference="rg_audit_bad_receipt_currency_v1",
            now=FROZEN_NOW,
        )


@pytest.mark.parametrize(
    ("collection", "component", "nested_currency"),
    [
        ("items", "item", "JPY"),
        ("items", "item", "XYZ"),
        ("items", "item", None),
        ("adjustments", "adjustment", "JPY"),
        ("adjustments", "adjustment", "XYZ"),
        ("adjustments", "adjustment", None),
    ],
)
def test_create_audit_snapshot_rejects_invalid_carried_nested_currency(
    collection: str,
    component: str,
    nested_currency: object,
) -> None:
    calc_result = _tc001_calc_result()
    calc_result["receipts"][0][collection][0]["currency"] = nested_currency

    with pytest.raises(
        AuditValidationError,
        match=rf"receipt\[0\] {component}\[0\] currency",
    ):
        create_audit_snapshot(
            calc_result,
            calculation_run_id="calc_audit_bad_nested_currency_v1",
            source_reference="rg_audit_bad_nested_currency_v1",
            now=FROZEN_NOW,
        )


@pytest.mark.parametrize("nested_currency", ["XYZ", None])
def test_create_audit_snapshot_wraps_invalid_embedded_settlement_currency(
    nested_currency: object,
) -> None:
    calc_result = _tc001_calc_result()
    calc_result["settlement_obligations"][0]["currency"] = nested_currency

    with pytest.raises(
        AuditValidationError,
        match=r"calculator output settlement_obligations\[0\] currency",
    ):
        create_audit_snapshot(
            calc_result,
            calculation_run_id="calc_audit_bad_embedded_currency_v1",
            source_reference="rg_audit_bad_embedded_currency_v1",
            now=FROZEN_NOW,
        )


@pytest.mark.parametrize("nested_currency", ["XYZ", None])
def test_create_audit_snapshot_wraps_invalid_override_settlement_currency(
    nested_currency: object,
) -> None:
    settlement_obligations = _tc001_settlement_obligations()
    settlement_obligations[0]["currency"] = nested_currency

    with pytest.raises(AuditValidationError, match=r"settlement override\[0\] currency"):
        _tc001_frozen_snapshot(settlement_obligations=settlement_obligations)


@pytest.mark.parametrize("embedded_currency", ["missing", None, "XYZ", "USD"])
@pytest.mark.parametrize("use_override", [False, True])
def test_create_audit_snapshot_always_validates_embedded_obligation_currency(
    embedded_currency: object,
    use_override: bool,
) -> None:
    calc_result = _tc001_calc_result()
    if embedded_currency == "missing":
        del calc_result["settlement_obligations"][0]["currency"]
    else:
        calc_result["settlement_obligations"][0]["currency"] = embedded_currency
    kwargs: dict[str, object] = {}
    if use_override:
        kwargs["settlement_obligations"] = _tc001_settlement_obligations()

    with pytest.raises(
        AuditValidationError,
        match=r"calculator output settlement_obligations\[0\] currency",
    ) as exc_info:
        create_audit_snapshot(
            calc_result,
            calculation_run_id="calc_audit_bad_embedded_obligation_v1",
            source_reference="rg_audit_bad_embedded_obligation_v1",
            now=FROZEN_NOW,
            **kwargs,  # type: ignore[arg-type]
        )

    if embedded_currency == "XYZ":
        assert isinstance(exc_info.value.__cause__, MoneyValidationError)


def test_create_audit_snapshot_requires_explicit_override_obligation_currency() -> None:
    settlement_obligations = _tc001_settlement_obligations()
    del settlement_obligations[0]["currency"]

    with pytest.raises(
        AuditValidationError,
        match=r"settlement override\[0\] currency",
    ):
        _tc001_frozen_snapshot(settlement_obligations=settlement_obligations)


# -- 2. Decimal values preserved --


def test_decimal_values_are_preserved_as_strings() -> None:
    snapshot = _tc001_frozen_snapshot()

    output = snapshot.output_snapshot
    for key in ("payer_own_share", "total_paid_by_payer", "total_to_collect", "total_paid"):
        val = output.get(key)
        assert isinstance(val, str), f"output[{key!r}] expected str, got {type(val).__name__}"

    for name, amount in output["participant_shares"].items():
        assert isinstance(amount, str), f"share[{name!r}] expected str, got {type(amount).__name__}"

    for obl in output["settlement_obligations"]:
        assert isinstance(obl["amount"], str)

    settlement = snapshot.settlement_snapshot
    assert isinstance(settlement["total_to_collect"], str)
    for obl in settlement["obligations"]:
        assert isinstance(obl["amount"], str)

    for entry in snapshot.rounding_snapshot:
        assert isinstance(entry["amount"], str)


# -- 3. Total paid = own share + collectable --


def test_total_paid_equals_own_share_plus_collectable() -> None:
    calc_result = _tc001_calc_result()

    payer_own = to_decimal_safe(calc_result["payer_own_share"])
    total_paid = to_decimal_safe(calc_result["total_paid_by_payer"])
    total_collect = to_decimal_safe(calc_result["total_to_collect"])

    assert total_paid == payer_own + total_collect

    snapshot = _tc001_frozen_snapshot()
    out_payer_own = to_decimal_safe(snapshot.output_snapshot["payer_own_share"])
    out_total_paid = to_decimal_safe(snapshot.output_snapshot["total_paid_by_payer"])
    out_total_collect = to_decimal_safe(snapshot.output_snapshot["total_to_collect"])

    assert out_total_paid == out_payer_own + out_total_collect


# -- 4. Settlement obligations sum correctly --


def test_settlement_obligations_sum_matches_total_to_collect() -> None:
    snapshot = _tc001_frozen_snapshot()

    settlement = snapshot.settlement_snapshot
    total_collect = to_decimal_safe(settlement["total_to_collect"])

    sum_obligations = sum(
        (to_decimal_safe(o["amount"]) for o in settlement["obligations"]),
        Decimal("0.00"),
    )

    assert sum_obligations == total_collect


# -- 5. Rounding adjustment recorded --


def test_rounding_adjustment_is_recorded() -> None:
    calc_result = _tc001_calc_result()

    # Find receipts with non-zero rounding
    receipts_with_rounding = [
        r
        for r in calc_result["receipts"]
        if r.get("rounding_adjustment") and r["rounding_adjustment"] != Decimal("0.00")
    ]

    snapshot = _tc001_frozen_snapshot()

    for receipt in receipts_with_rounding:
        # Ensure a matching entry exists in the rounding snapshot
        label = receipt.get("merchant") or receipt.get("label") or ""
        found = any(entry.get("receipt_label") == label for entry in snapshot.rounding_snapshot)
        assert found, (
            f"No rounding entry found for receipt {label!r} "
            f"which has rounding adjustment {receipt['rounding_adjustment']}"
        )


# -- 6. Missing source reference --


def test_missing_source_reference_fails() -> None:
    with pytest.raises(AuditValidationError, match="source_reference is required"):
        AuditSnapshot(
            calculation_run_id=TEST_AUDIT_CALC_RUN_ID,
            source_type="receipt_finalization",
            source_reference="",
            status="calculated",
            currency="SGD",
            created_at=FROZEN_NOW,
        )


# -- 7. Missing currency --


def test_missing_currency_fails() -> None:
    with pytest.raises(AuditValidationError, match="currency"):
        AuditSnapshot(
            calculation_run_id=TEST_AUDIT_CALC_RUN_ID,
            source_type="receipt_finalization",
            source_reference=TEST_SOURCE_REF,
            status="calculated",
            currency="",
            created_at=FROZEN_NOW,
        )


# -- 8. Float monetary input rejected --


def test_float_monetary_value_in_output_snapshot_is_rejected() -> None:
    """Snapshot containing a float in output_snapshot must be rejected."""
    with pytest.raises(AuditValidationError, match="float"):
        AuditSnapshot(
            calculation_run_id=TEST_AUDIT_CALC_RUN_ID,
            source_type="receipt_finalization",
            source_reference=TEST_SOURCE_REF,
            status="calculated",
            currency="SGD",
            output_snapshot={"total_paid": 70.12},
            created_at=FROZEN_NOW,
        )


def test_float_monetary_value_in_input_snapshot_is_rejected() -> None:
    with pytest.raises(AuditValidationError, match="float"):
        AuditSnapshot(
            calculation_run_id=TEST_AUDIT_CALC_RUN_ID,
            source_type="receipt_finalization",
            source_reference=TEST_SOURCE_REF,
            status="calculated",
            currency="SGD",
            input_snapshot={"subtotal": 66.60},
            created_at=FROZEN_NOW,
        )


def test_float_in_settlement_snapshot_obligations_is_rejected() -> None:
    with pytest.raises(AuditValidationError, match="float"):
        AuditSnapshot(
            calculation_run_id=TEST_AUDIT_CALC_RUN_ID,
            source_type="receipt_finalization",
            source_reference=TEST_SOURCE_REF,
            status="calculated",
            currency="SGD",
            settlement_snapshot={
                "payer": "Owner",
                "currency": "SGD",
                "obligation_count": 1,
                "total_to_collect": "14.91",
                "obligations": [
                    {"debtor": "MemberA", "creditor": "Owner", "amount": 14.91, "currency": "SGD"}
                ],
            },
            created_at=FROZEN_NOW,
        )


# -- 9. Deterministic timestamp injection --


def test_deterministic_timestamp_injection_works() -> None:
    snapshot = _tc001_frozen_snapshot(now="2026-01-01T00:00:00Z")
    assert snapshot.created_at == "2026-01-01T00:00:00Z"

    snapshot2 = create_audit_snapshot(
        _tc001_calc_result(),
        calculation_run_id="calc_ts_test",
        source_reference=TEST_SOURCE_REF,
        now="2025-12-31T23:59:59+00:00",
    )
    assert snapshot2.created_at == "2025-12-31T23:59:59+00:00"


def test_timestamp_defaults_to_iso_format() -> None:
    """When no explicit timestamp is given, created_at is an ISO-formatted string."""
    snapshot = create_audit_snapshot(
        _tc001_calc_result(),
        calculation_run_id="calc_ts_default",
        source_reference=TEST_SOURCE_REF,
    )
    # Must be a non-empty ISO-8601-compatible string
    assert isinstance(snapshot.created_at, str)
    assert "T" in snapshot.created_at
    assert len(snapshot.created_at) >= 20


# -- 10. Original calc result not mutated --


def test_original_calc_result_is_not_mutated() -> None:
    calc_result = _tc001_calc_result()
    original = deepcopy(calc_result)

    create_audit_snapshot(
        calc_result,
        calculation_run_id=TEST_AUDIT_CALC_RUN_ID,
        source_reference=TEST_SOURCE_REF,
        now=FROZEN_NOW,
    )

    assert calc_result == original
    # Spot-check that Decimal values are still Decimal
    assert isinstance(calc_result["participant_shares"]["person_owner"], Decimal)
    assert isinstance(calc_result["settlement_obligations"][0]["amount"], Decimal)


# -- 11. Invalid status rejected --


def test_invalid_audit_status_is_rejected() -> None:
    with pytest.raises(AuditValidationError, match="Invalid audit status"):
        AuditSnapshot(
            calculation_run_id=TEST_AUDIT_CALC_RUN_ID,
            source_type="receipt_finalization",
            source_reference=TEST_SOURCE_REF,
            status="bogus_status",
            currency="SGD",
            created_at=FROZEN_NOW,
        )


# -- 12. Settlement obligations must balance to total_to_collect --


def test_unbalanced_settlement_obligations_are_rejected() -> None:
    with pytest.raises(AuditValidationError, match="does not match total_to_collect"):
        AuditSnapshot(
            calculation_run_id=TEST_AUDIT_CALC_RUN_ID,
            source_type="receipt_finalization",
            source_reference=TEST_SOURCE_REF,
            status="calculated",
            currency="SGD",
            output_snapshot={
                "total_paid": "70.12",
                "payer_own_share": "13.55",
                "total_to_collect": "56.57",
            },
            settlement_snapshot={
                "payer": "Owner",
                "currency": "SGD",
                "obligation_count": 2,
                "total_to_collect": "100.00",  # Does not match sum below
                "obligations": [
                    {
                        "debtor": "MemberA",
                        "creditor": "Owner",
                        "amount": "14.91",
                        "currency": "SGD",
                    },
                    {
                        "debtor": "MemberB",
                        "creditor": "Owner",
                        "amount": "17.54",
                        "currency": "SGD",
                    },
                ],
            },
            created_at=FROZEN_NOW,
        )


def test_audit_rejects_same_participants_with_different_amounts() -> None:
    settlement_obligations = _tc001_settlement_obligations()
    settlement_obligations[0]["amount"] = Decimal("15.91")
    settlement_obligations[1]["amount"] = Decimal("16.54")

    with pytest.raises(AuditValidationError, match="settlement obligations do not match output"):
        _tc001_frozen_snapshot(settlement_obligations=settlement_obligations)


def test_audit_rejects_same_participants_and_amounts_with_different_currency() -> None:
    settlement_obligations = _tc001_settlement_obligations()
    settlement_obligations[0]["currency"] = "USD"

    with pytest.raises(AuditValidationError, match=r"settlement override\[0\] currency"):
        _tc001_frozen_snapshot(settlement_obligations=settlement_obligations)


@pytest.mark.parametrize("obligation_currency", ["USD", "XYZ"])
def test_validate_audit_snapshot_rejects_consistent_wrong_obligation_currency(
    obligation_currency: str,
) -> None:
    obligation = {
        "debtor": "MemberA",
        "creditor": "Owner",
        "amount": "1.00",
        "currency": obligation_currency,
    }
    snapshot = AuditSnapshot(
        calculation_run_id="calc_audit_direct_wrong_currency_v1",
        source_type="receipt_finalization",
        source_reference="rg_audit_direct_wrong_currency_v1",
        status="calculated",
        currency="SGD",
        output_snapshot={
            "currency": "SGD",
            "settlement_obligations": [dict(obligation)],
        },
        settlement_snapshot={
            "currency": "SGD",
            "obligation_count": 1,
            "total_to_collect": "1.00",
            "obligations": [dict(obligation)],
        },
        created_at=FROZEN_NOW,
    )

    with pytest.raises(AuditValidationError, match="obligation.*currency"):
        validate_audit_snapshot(snapshot)


def test_validate_audit_snapshot_rejects_mutated_nested_obligation_currency() -> None:
    snapshot = _tc001_frozen_snapshot()
    snapshot.output_snapshot["settlement_obligations"][0]["currency"] = "XYZ"
    snapshot.settlement_snapshot["obligations"][0]["currency"] = "XYZ"

    with pytest.raises(AuditValidationError, match="obligation.*currency"):
        validate_audit_snapshot(snapshot)


@pytest.mark.parametrize("section", ["output_snapshot", "settlement_snapshot"])
@pytest.mark.parametrize("section_currency", ["missing", "USD", "XYZ"])
def test_validate_audit_snapshot_rejects_invalid_section_currency(
    section: str,
    section_currency: str,
) -> None:
    snapshot = _tc001_frozen_snapshot()
    target = getattr(snapshot, section)
    if section_currency == "missing":
        del target["currency"]
    else:
        target["currency"] = section_currency

    with pytest.raises(AuditValidationError, match=rf"{section} currency"):
        validate_audit_snapshot(snapshot)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("missing", "output_snapshot has 5 obligations but settlement_snapshot has 4"),
        ("extra", "output_snapshot has 5 obligations but settlement_snapshot has 6"),
    ],
)
def test_audit_rejects_missing_or_extra_obligation(mutation: str, match: str) -> None:
    settlement_obligations = _tc001_settlement_obligations()
    if mutation == "missing":
        settlement_obligations = settlement_obligations[:-1]
    else:
        settlement_obligations.append(
            {"debtor": "MemberA", "creditor": "Owner", "amount": Decimal("0.01"), "currency": "SGD"}
        )

    with pytest.raises(AuditValidationError, match=match):
        _tc001_frozen_snapshot(settlement_obligations=settlement_obligations)


def test_audit_accepts_identical_obligations_in_different_order() -> None:
    settlement_obligations = list(reversed(_tc001_settlement_obligations()))

    snapshot = _tc001_frozen_snapshot(settlement_obligations=settlement_obligations)

    assert snapshot.settlement_snapshot["obligation_count"] == 5


def test_audit_rejects_shifted_amounts_when_total_remains_same() -> None:
    settlement_obligations = _tc001_settlement_obligations()
    settlement_obligations[0]["amount"] += Decimal("1.00")
    settlement_obligations[1]["amount"] -= Decimal("1.00")

    with pytest.raises(AuditValidationError, match="settlement obligations do not match output"):
        _tc001_frozen_snapshot(settlement_obligations=settlement_obligations)


# -- 13. Required calculation_run_id --


def test_missing_calculation_run_id_is_rejected() -> None:
    with pytest.raises(AuditValidationError, match="calculation_run_id is required"):
        AuditSnapshot(
            calculation_run_id="",
            source_type="receipt_finalization",
            source_reference=TEST_SOURCE_REF,
            status="calculated",
            currency="SGD",
            created_at=FROZEN_NOW,
        )


# -- 14. Missing created_at timestamp --


def test_missing_created_at_is_rejected() -> None:
    with pytest.raises(AuditValidationError, match="created_at timestamp is required"):
        AuditSnapshot(
            calculation_run_id=TEST_AUDIT_CALC_RUN_ID,
            source_type="receipt_finalization",
            source_reference=TEST_SOURCE_REF,
            status="calculated",
            currency="SGD",
            created_at="",
        )


# -- 15. validate_audit_snapshot rejects unbalanced totals --


def test_validate_rejects_unbalanced_total_reconciliation() -> None:
    """Unbalanced reconciliation is rejected at construction time."""
    with pytest.raises(AuditValidationError, match="Total to collect"):
        AuditSnapshot(
            calculation_run_id=TEST_AUDIT_CALC_RUN_ID,
            source_type="receipt_finalization",
            source_reference=TEST_SOURCE_REF,
            status="calculated",
            currency="SGD",
            output_snapshot={
                "total_paid": "70.12",
                "payer_own_share": "13.55",
                "total_to_collect": "99.99",  # wrong
            },
            created_at=FROZEN_NOW,
        )


# -- 16. Evidence references stored --


def test_evidence_references_are_stored() -> None:
    calc_result = _tc001_calc_result()
    snapshot = create_audit_snapshot(
        calc_result,
        calculation_run_id=TEST_AUDIT_CALC_RUN_ID,
        source_reference=TEST_SOURCE_REF,
        evidence_references=("attach_001", "attach_002"),
        now=FROZEN_NOW,
    )

    assert snapshot.evidence_references == ("attach_001", "attach_002")


# -- 17. Input snapshot captures case metadata --


def test_input_snapshot_captures_case_metadata() -> None:
    snapshot = _tc001_frozen_snapshot()
    inp = snapshot.input_snapshot

    assert inp["case_id"] == "TC001_receipt_split_example_restaurant_example_tea"
    assert inp["currency"] == "SGD"
    assert inp["participants"] == [
        "person_owner",
        "person_member_a",
        "person_member_b",
        "person_member_c",
        "person_member_d",
        "person_member_e",
    ]
    assert inp["payer"] == "person_owner"
    assert inp["receipt_count"] == 2


# -- 18. Rules snapshot captures applied methods --


def test_rules_snapshot_captures_applied_methods() -> None:
    snapshot = _tc001_frozen_snapshot()
    rules = snapshot.rules_snapshot

    assert rules["version"] == "receipt_finalization_v1"
    assert rules["rounding_policy"] == "payer_following"
    assert rules["rounding_method"] == "ROUND_HALF_UP"
    assert rules["rounding_quantum"] == "0.01"

    # TC001 uses equal_per_participant and proportional_by_item_amount for discount,
    # and proportional_by_item_amount for service charge
    assert "equal_per_participant" in rules.get("discount_allocation_methods", [])
    assert "proportional_by_item_amount" in rules.get("discount_allocation_methods", [])
    assert "proportional_by_item_amount" in rules.get("service_charge_allocation_methods", [])


# -- 19. Rounding amount validation --


def test_rounding_entry_invalid_decimal_is_rejected() -> None:
    from finance_core.calculation_audit.models import AuditRoundingEntry

    with pytest.raises(AuditValidationError, match="must be Decimal"):
        AuditRoundingEntry(participant="Owner", amount=0.01, policy="payer")  # type: ignore[arg-type]


def test_rounding_entry_missing_participant_is_rejected() -> None:
    from finance_core.calculation_audit.models import AuditRoundingEntry

    with pytest.raises(AuditValidationError, match="Rounding participant is required"):
        AuditRoundingEntry(participant="", amount=Decimal("0.01"), policy="payer")


def test_settlement_entry_self_obligation_is_rejected() -> None:
    from finance_core.calculation_audit.models import AuditSettlementEntry

    with pytest.raises(AuditValidationError, match="Self-obligation"):
        AuditSettlementEntry(
            debtor="Owner", creditor="Owner", amount=Decimal("1.00"), currency="SGD"
        )


def test_settlement_entry_negative_amount_is_rejected() -> None:
    from finance_core.calculation_audit.models import AuditSettlementEntry

    with pytest.raises(AuditValidationError, match="must be positive"):
        AuditSettlementEntry(
            debtor="MemberA", creditor="Owner", amount=Decimal("-1.00"), currency="SGD"
        )


# -- 20. to_decimal_safe rejects float --


def test_to_decimal_safe_rejects_float() -> None:
    with pytest.raises(AuditValidationError, match="float"):
        to_decimal_safe(14.91)


def test_to_decimal_safe_accepts_decimal() -> None:
    assert to_decimal_safe(Decimal("14.91")) == Decimal("14.91")


def test_to_decimal_safe_accepts_string() -> None:
    assert to_decimal_safe("14.91") == Decimal("14.91")


def test_to_decimal_safe_rejects_none() -> None:
    with pytest.raises(AuditValidationError, match="required"):
        to_decimal_safe(None)
