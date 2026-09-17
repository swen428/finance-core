"""Focused Money Contract tests for reconciliation final-transaction CREATE."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

import finance_core.reconciliation.final_mutation_money as create_money_module
from finance_core.reconciliation.final_mutation_authorization import (
    build_final_mutation_content_hash,
)
from finance_core.reconciliation.final_mutation_money import (
    CreateMoneyValidationError,
    CreateMoneyValidationIssue,
    validate_reconciliation_create_money,
)
from finance_core.reconciliation.final_mutation_proposal import (
    FinalMutationAction,
    FinalMutationBlockedReason,
    FinalMutationGuard,
    FinalMutationProposal,
)


class _HostileUnsupportedAmount:
    def __repr__(self) -> str:
        raise RuntimeError("repr must not be required")

    def __str__(self) -> str:
        raise RuntimeError("str must not be required")


def _proposal(**changes: Any) -> FinalMutationProposal:
    values: dict[str, Any] = {
        "proposal_id": "create-money-proposal-1",
        "action": FinalMutationAction.CREATE_FINAL_TRANSACTION,
        "amount": Decimal("12.30"),
        "currency": "SGD",
        "merchant": "Money Contract Merchant",
        "transaction_date": date(2026, 7, 16),
        "source_statement_ref": "statement-money-1",
        "evidence_refs": ("evidence-money-1",),
    }
    values.update(changes)
    return FinalMutationProposal(**values)


@pytest.mark.parametrize(
    ("amount", "currency", "canonical_amount", "normalized_currency"),
    [
        (Decimal("12.3"), "SGD", "12.30", "SGD"),
        (Decimal("12.30"), " sgd ", "12.30", "SGD"),
        (Decimal("100.0"), "JPY", "100", "JPY"),
    ],
)
def test_validator_returns_immutable_canonical_create_money(
    amount: Decimal,
    currency: str,
    canonical_amount: str,
    normalized_currency: str,
) -> None:
    validated = validate_reconciliation_create_money(amount, currency)

    assert validated.amount == amount
    assert validated.currency == normalized_currency
    assert validated.canonical_amount == canonical_amount
    with pytest.raises(AttributeError):
        validated.currency = "USD"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("amount", "currency"),
    [
        (12.3, "SGD"),
        (True, "SGD"),
        (None, "SGD"),
        (Decimal("NaN"), "SGD"),
        (Decimal("Infinity"), "SGD"),
        (Decimal("-Infinity"), "SGD"),
        ("NaN", "SGD"),
        ("Infinity", "SGD"),
        ("1e2", "SGD"),
        ("", "SGD"),
        (object(), "SGD"),
        (_HostileUnsupportedAmount(), "SGD"),
        (0, "SGD"),
        (Decimal("-1.00"), "SGD"),
        (Decimal("12.345"), "SGD"),
        (Decimal("1.5"), "JPY"),
    ],
)
def test_validator_rejects_invalid_create_amount_without_rounding(
    amount: object,
    currency: str,
) -> None:
    with pytest.raises(CreateMoneyValidationError) as exc_info:
        validate_reconciliation_create_money(amount, currency)

    assert CreateMoneyValidationIssue.AMOUNT in exc_info.value.issues


@pytest.mark.parametrize(
    "currency",
    [None, "", "   ", "XYZ", "SG", "S1D", object()],
)
def test_validator_rejects_invalid_create_currency(currency: object) -> None:
    with pytest.raises(CreateMoneyValidationError) as exc_info:
        validate_reconciliation_create_money(Decimal("12.30"), currency)

    assert CreateMoneyValidationIssue.CURRENCY in exc_info.value.issues


@pytest.mark.parametrize(
    "amount",
    [
        12.3,
        True,
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        "NaN",
        "Infinity",
        "1e2",
        "",
        object(),
        _HostileUnsupportedAmount(),
        0,
        Decimal("-1.00"),
        Decimal("12.345"),
    ],
)
def test_guard_blocks_invalid_create_amount_without_raising(amount: object) -> None:
    decision = FinalMutationGuard().evaluate(_proposal(amount=amount))

    assert decision.approved is False
    assert decision.preview is None
    assert decision.blocked_reasons == (FinalMutationBlockedReason.INVALID_AMOUNT.value,)


@pytest.mark.parametrize("currency", ["   ", "XYZ", "SG", "S1D", object()])
def test_guard_blocks_invalid_create_currency_without_raising(currency: object) -> None:
    decision = FinalMutationGuard().evaluate(_proposal(currency=currency))

    assert decision.approved is False
    assert decision.preview is None
    assert decision.blocked_reasons == (FinalMutationBlockedReason.INVALID_CURRENCY.value,)


def test_guard_preserves_missing_money_reasons_and_deterministic_order() -> None:
    decision = FinalMutationGuard().evaluate(
        _proposal(amount=None, currency=None, transaction_date=None, merchant=None)
    )

    assert decision.blocked_reasons[:4] == (
        FinalMutationBlockedReason.MISSING_AMOUNT.value,
        FinalMutationBlockedReason.MISSING_CURRENCY.value,
        FinalMutationBlockedReason.MISSING_DATE.value,
        FinalMutationBlockedReason.MISSING_MERCHANT.value,
    )


def test_guard_reports_both_invalid_money_fields_in_deterministic_order() -> None:
    decision = FinalMutationGuard().evaluate(_proposal(amount=0, currency="XYZ"))

    assert decision.blocked_reasons == (
        FinalMutationBlockedReason.INVALID_AMOUNT.value,
        FinalMutationBlockedReason.INVALID_CURRENCY.value,
    )


def test_guard_preview_uses_canonical_amount_and_normalized_currency() -> None:
    decision = FinalMutationGuard().evaluate(_proposal(amount=Decimal("12.3"), currency=" sgd "))

    assert decision.approved is True
    assert decision.preview is not None
    assert decision.preview.amount == "12.30"
    assert decision.preview.currency == "SGD"


def test_valid_equivalent_create_money_has_equivalent_content_hash() -> None:
    canonical = build_final_mutation_content_hash(_proposal(amount=Decimal("12.30")))

    assert build_final_mutation_content_hash(_proposal(amount=Decimal("12.3"))) == canonical
    assert build_final_mutation_content_hash(_proposal(currency=" sgd ")) == canonical


def test_material_create_money_changes_content_hash() -> None:
    original = build_final_mutation_content_hash(_proposal())

    assert build_final_mutation_content_hash(_proposal(amount=Decimal("12.31"))) != original
    assert build_final_mutation_content_hash(_proposal(currency="USD")) != original


@pytest.mark.parametrize(
    ("amount", "currency"),
    [
        (Decimal("12.345"), "SGD"),
        (Decimal("1.5"), "JPY"),
        (12.3, "SGD"),
        (Decimal("12.30"), "XYZ"),
    ],
)
def test_invalid_create_money_cannot_obtain_content_hash(
    amount: object,
    currency: object,
) -> None:
    with pytest.raises(CreateMoneyValidationError):
        build_final_mutation_content_hash(_proposal(amount=amount, currency=currency))


@pytest.mark.parametrize(
    "dependency_name",
    [
        "normalize_currency",
        "money_decimal",
        "validate_amount_for_currency",
        "canonical_money_str",
    ],
)
def test_validator_propagates_unexpected_money_contract_failures(
    dependency_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_failure(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(f"unexpected {dependency_name} failure")

    monkeypatch.setattr(create_money_module, dependency_name, unexpected_failure)

    with pytest.raises(RuntimeError, match=f"unexpected {dependency_name} failure"):
        validate_reconciliation_create_money(Decimal("12.30"), "SGD")
