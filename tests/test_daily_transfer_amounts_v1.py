import pytest

from finance_core.lifecycle_v2 import LifecycleError
from finance_core.lifecycle_v2.daily_v1.contracts import AccountDefinitionRefV1
from finance_core.lifecycle_v2.daily_v1.transfer_amounts_v1 import (
    TransferAccountAmountV1,
    TransferAmountsV1,
    TransferFeeV1,
)


def account_amount(account_id, currency, amount, *, version=1):
    return TransferAccountAmountV1(
        AccountDefinitionRefV1(account_id, version, "a" * 64), currency, amount
    )


def impacts(transfer):
    return tuple(
        (item.role, item.account.account_id, item.currency, item.amount)
        for item in transfer.account_impacts()
    )


def test_cross_currency_transfer_keeps_principal_and_separate_fee_account_impacts():
    transfer = TransferAmountsV1(
        account_amount("bank-sgd", "SGD", "100"),
        account_amount("bank-myr", "MYR", "330"),
        TransferFeeV1("separate_debit", account_amount("bank-sgd", "SGD", "1")),
    )

    assert impacts(transfer) == (
        ("transfer_out", "bank-sgd", "SGD", "-100.00"),
        ("transfer_in", "bank-myr", "MYR", "330.00"),
        ("fee_debit", "bank-sgd", "SGD", "-1.00"),
    )
    assert transfer.expense_by_currency() == (("SGD", "1.00"),)


@pytest.mark.parametrize(
    "mode,account_id", [("included_in_out", "out"), ("withheld_from_in", "in")]
)
def test_fee_in_a_leg_has_one_expense_and_no_second_account_debit(mode, account_id):
    transfer = TransferAmountsV1(
        account_amount("out", "SGD", "101"),
        account_amount("in", "SGD", "100"),
        TransferFeeV1(mode, account_amount(account_id, "SGD", "1")),
    )

    assert impacts(transfer) == (
        ("transfer_out", "out", "SGD", "-101.00"),
        ("transfer_in", "in", "SGD", "100.00"),
    )
    assert transfer.expense_by_currency() == (("SGD", "1.00"),)


def test_separate_fee_can_use_a_third_actual_account():
    transfer = TransferAmountsV1(
        account_amount("out", "JPY", "2000"),
        account_amount("in", "SGD", "18.30"),
        TransferFeeV1("separate_debit", account_amount("fees", "MYR", "2.00")),
    )

    assert impacts(transfer)[-1] == ("fee_debit", "fees", "MYR", "-2.00")
    assert transfer.expense_by_currency() == (("MYR", "2.00"),)


def test_same_currency_repayment_without_a_fee_contributes_no_expense():
    transfer = TransferAmountsV1(
        account_amount("bank", "SGD", "50"),
        account_amount("credit-card", "SGD", "50"),
        TransferFeeV1("none"),
    )

    assert impacts(transfer) == (
        ("transfer_out", "bank", "SGD", "-50.00"),
        ("transfer_in", "credit-card", "SGD", "50.00"),
    )
    assert transfer.expense_by_currency() == ()


@pytest.mark.parametrize("amount", ["0", "-1", "12.34"])
def test_jpy_transfer_rejects_nonpositive_or_overprecision_amounts(amount):
    with pytest.raises(LifecycleError):
        account_amount("bank-jpy", "JPY", amount)


def test_transfer_refuses_unexplained_same_currency_difference_and_same_account():
    with pytest.raises(LifecycleError, match="differ from the stated fee"):
        TransferAmountsV1(
            account_amount("out", "SGD", "100"),
            account_amount("in", "SGD", "99"),
            TransferFeeV1("none"),
        )

    with pytest.raises(LifecycleError, match="distinct"):
        TransferAmountsV1(
            account_amount("bank", "SGD", "100"),
            account_amount("bank", "SGD", "100", version=2),
            TransferFeeV1("none"),
        )


def test_transfer_refuses_unknown_or_inconsistently_bound_fees():
    with pytest.raises(LifecycleError, match="actual amount and account"):
        TransferFeeV1("separate_debit")
    with pytest.raises(LifecycleError, match="without fees"):
        TransferFeeV1("none", account_amount("out", "SGD", "1"))

    outgoing = account_amount("out", "SGD", "101")
    incoming = account_amount("in", "MYR", "330")
    with pytest.raises(LifecycleError, match="stated account and currency"):
        TransferAmountsV1(
            outgoing,
            incoming,
            TransferFeeV1("included_in_out", account_amount("out", "SGD", "1", version=2)),
        )
    with pytest.raises(LifecycleError, match="stated account and currency"):
        TransferAmountsV1(
            outgoing, incoming, TransferFeeV1("withheld_from_in", account_amount("out", "SGD", "1"))
        )
    with pytest.raises(LifecycleError, match="positive outgoing principal"):
        TransferAmountsV1(
            outgoing,
            incoming,
            TransferFeeV1("included_in_out", account_amount("out", "SGD", "101")),
        )
