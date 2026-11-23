"""Actual account amounts and fee projection for one daily transfer."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
from typing import Literal, cast, get_args

from finance_core.money import SUPPORTED_CURRENCIES, MoneyValidationError

from ..contracts import LifecycleError
from .contracts import AccountDefinitionRefV1, _money

TransferFeeMode = Literal["none", "separate_debit", "included_in_out", "withheld_from_in"]
TransferRole = Literal["transfer_out", "transfer_in", "fee_debit"]
_FEE_MODES = frozenset(get_args(TransferFeeMode))


def _fee_mode(value: object) -> TransferFeeMode:
    if type(value) is not str or value not in _FEE_MODES:
        raise LifecycleError("Exact transfer fee treatment required")
    return cast(TransferFeeMode, value)


@dataclass(frozen=True)
class TransferAccountAmountV1:
    account: AccountDefinitionRefV1
    currency: str
    amount: str

    def __post_init__(self) -> None:
        if type(self.account) is not AccountDefinitionRefV1:
            raise LifecycleError("Exact transfer account reference required")
        if type(self.currency) is not str or self.currency not in SUPPORTED_CURRENCIES:
            raise LifecycleError("Exact supported transfer currency required")
        try:
            amount = _money(self.amount, self.currency)
        except MoneyValidationError:
            raise LifecycleError(
                "Transfer amount exceeds its currency precision or range"
            ) from None
        if not Decimal(amount) > 0:
            raise LifecycleError("Transfer account amount must be positive")
        object.__setattr__(self, "amount", amount)


@dataclass(frozen=True)
class TransferFeeV1:
    mode: TransferFeeMode
    charge: TransferAccountAmountV1 | None = None

    def __post_init__(self) -> None:
        _fee_mode(self.mode)
        if self.mode == "none":
            if self.charge is not None:
                raise LifecycleError("A transfer without fees cannot carry a fee debit")
        elif type(self.charge) is not TransferAccountAmountV1:
            raise LifecycleError("A transfer fee requires its actual amount and account")


@dataclass(frozen=True)
class TransferAccountImpactV1:
    role: TransferRole
    account: AccountDefinitionRefV1
    currency: str
    amount: str


@dataclass(frozen=True)
class TransferAmountsV1:
    outgoing: TransferAccountAmountV1
    incoming: TransferAccountAmountV1
    fee: TransferFeeV1

    def __post_init__(self) -> None:
        if (
            type(self.outgoing) is not TransferAccountAmountV1
            or type(self.incoming) is not TransferAccountAmountV1
            or type(self.fee) is not TransferFeeV1
        ):
            raise LifecycleError(
                "Both actual transfer legs and an explicit fee treatment are required"
            )
        if self.outgoing.account.account_id == self.incoming.account.account_id:
            raise LifecycleError("A transfer requires distinct outgoing and incoming accounts")

        charge = self.fee.charge
        out_value = Decimal(self.outgoing.amount)
        if charge is not None:
            if self.fee.mode == "included_in_out":
                self._require_fee_leg(charge, self.outgoing)
                if Decimal(charge.amount) >= out_value:
                    raise LifecycleError("An included fee must leave a positive outgoing principal")
            elif self.fee.mode == "withheld_from_in":
                self._require_fee_leg(charge, self.incoming)
            else:
                for leg in (self.outgoing, self.incoming):
                    if charge.account.account_id == leg.account.account_id:
                        self._require_fee_leg(charge, leg)

        if self.outgoing.currency == self.incoming.currency:
            expected_fee = (
                Decimal(charge.amount)
                if charge is not None and self.fee.mode in {"included_in_out", "withheld_from_in"}
                else Decimal(0)
            )
            with localcontext(Context(prec=256)):
                if out_value - Decimal(self.incoming.amount) != expected_fee:
                    raise LifecycleError(
                        "Same-currency transfer amounts differ from the stated fee"
                    )

    @staticmethod
    def _require_fee_leg(charge: TransferAccountAmountV1, leg: TransferAccountAmountV1) -> None:
        if charge.account != leg.account or charge.currency != leg.currency:
            raise LifecycleError("Transfer fee does not match its stated account and currency")

    def account_impacts(self) -> tuple[TransferAccountImpactV1, ...]:
        result = (
            TransferAccountImpactV1(
                "transfer_out",
                self.outgoing.account,
                self.outgoing.currency,
                "-" + self.outgoing.amount,
            ),
            TransferAccountImpactV1(
                "transfer_in", self.incoming.account, self.incoming.currency, self.incoming.amount
            ),
        )
        charge = self.fee.charge
        if self.fee.mode == "separate_debit" and charge is not None:
            return (
                *result,
                TransferAccountImpactV1(
                    "fee_debit", charge.account, charge.currency, "-" + charge.amount
                ),
            )
        return result

    def expense_by_currency(self) -> tuple[tuple[str, str], ...]:
        charge = self.fee.charge
        return () if charge is None else ((charge.currency, charge.amount),)
