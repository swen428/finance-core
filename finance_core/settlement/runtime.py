"""Settlement Obligation Runtime v1.

Pure deterministic generation of settlement obligations from participant
balances. No persistence, no SQLite access, no file I/O.

Algorithm
---------

1. Partition participants into creditors (balance > 0) and debtors (balance < 0).
2. Sort both groups deterministically by (amount descending, participant_id).
3. Greedily match: for each creditor in order, satisfy the debt of the
   first remaining debtor.  When a debtor is fully satisfied, advance to
   the next debtor.  When a creditor is fully allocated, advance to the
   next creditor.
4. Produce SettlementObligation records for every matched pair with
   amount > 0.

The deterministic sort order ensures that the same inputs always produce
the same outputs in the same order, regardless of Python interpreter or
operating system.

Relationship to Future Persistence Layer
----------------------------------------

This runtime is the calculation engine.  A future persistence layer
(e.g. ``finance_core/settlement/persistence.py``) will consume
``SettlementObligation`` records produced here and write them to SQLite,
add settlement status tracking, payment reconciliation, and reporting.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from finance_core.money import (
    ZERO,
    MoneyValidationError,
    money_decimal,
    quantize_for_currency,
    validate_amount_for_currency,
)


def _round_for_currency(value: Decimal, currency: str) -> Decimal:
    """Quantize to the currency's minor-unit scale using ROUND_HALF_UP."""
    return quantize_for_currency(value, currency)


@dataclass(frozen=True)
class SettlementObligation:
    """A single deterministic settlement obligation from debtor to creditor.

    All monetary values use ``Decimal``; floats are forbidden.
    The dataclass is frozen (immutable) to prevent accidental mutation
    after generation.
    """

    obligation_id: str
    creditor_id: str
    debtor_id: str
    amount: Decimal
    currency: str
    source_type: str
    source_id: str

    def __post_init__(self) -> None:
        if not self.obligation_id:
            raise ValueError("obligation_id is required")
        if not self.creditor_id:
            raise ValueError("creditor_id is required")
        if not self.debtor_id:
            raise ValueError("debtor_id is required")
        if self.creditor_id == self.debtor_id:
            raise ValueError(
                f"Self-obligation is not allowed: {self.debtor_id} cannot owe {self.creditor_id}"
            )
        if not isinstance(self.amount, Decimal):
            raise ValueError(f"Amount must be Decimal, got {type(self.amount).__name__}")
        if self.amount <= ZERO:
            raise ValueError(f"Obligation amount must be positive, got {self.amount}")
        # Validate amount does not exceed currency scale.
        validate_amount_for_currency(
            self.amount,
            self.currency,
            label=f"obligation {self.debtor_id}->{self.creditor_id}",
        )
        if not self.currency or not isinstance(self.currency, str):
            raise ValueError("currency is required and must be a non-empty string")
        if not self.source_type or not isinstance(self.source_type, str):
            raise ValueError("source_type is required and must be a non-empty string")
        if not self.source_id or not isinstance(self.source_id, str):
            raise ValueError("source_id is required and must be a non-empty string")


@dataclass(frozen=True)
class _BalanceEntry:
    """Internal holder for a participant's current balance during matching."""

    participant_id: str
    amount: Decimal  # always non-negative


def _build_balance_entries(
    participants: list[str],
    balances: dict[str, Decimal],
) -> tuple[list[_BalanceEntry], list[_BalanceEntry]]:
    """Build sorted debtor and creditor entries from participant balances.

    Returns (creditors, debtors) sorted deterministically by
    (amount descending, participant_id ascending).
    """
    creditors: list[_BalanceEntry] = []
    debtors: list[_BalanceEntry] = []

    for participant in participants:
        balance = balances.get(participant, ZERO)
        if balance > ZERO:
            creditors.append(_BalanceEntry(participant_id=participant, amount=balance))
        elif balance < ZERO:
            debtors.append(_BalanceEntry(participant_id=participant, amount=-balance))

    # Deterministic sort: amount descending, then participant_id ascending.
    creditors.sort(key=lambda e: (-e.amount, e.participant_id))
    debtors.sort(key=lambda e: (-e.amount, e.participant_id))

    return creditors, debtors


class SettlementRuntime:
    """Deterministic settlement obligation generator.

    This is a pure runtime: it takes participant balances and produces
    SettlementObligation records.  It never touches the database,
    filesystem, or network.

    Usage::

        runtime = SettlementRuntime()
        obligations = runtime.generate_obligations(
            participants=["person_owner", "person_member_a", "person_member_b"],
            balances={
                "person_owner": Decimal("60"),
                "person_member_a": Decimal("-30"),
                "person_member_b": Decimal("-30"),
            },
            currency="SGD",
            source_type="receipt_split",
            source_id="calc_001",
        )
    """

    def generate_obligations(
        self,
        *,
        participants: list[str],
        balances: dict[str, Decimal],
        currency: str,
        source_type: str,
        source_id: str,
    ) -> list[SettlementObligation]:
        """Generate deterministic settlement obligations from participant balances.

        Parameters
        ----------
        participants:
            Ordered list of participant identifiers.
        balances:
            Map of participant_id to net balance (paid minus share).
            Positive balance = creditor (is owed money).
            Negative balance = debtor (owes money).
            Zero balance = settled, produces no obligations.
        currency:
            ISO 4217 currency code (e.g. "SGD").
        source_type:
            Identifier for the source system (e.g. "receipt_split").
        source_id:
            Identifier for the specific source record.

        Returns
        -------
        Deterministic list of SettlementObligation records sorted by
        (creditor_id, debtor_id).

        Raises
        ------
        ValueError
            If balances do not sum to zero, if participants are invalid,
            or if required parameters are missing.
        """
        self._validate_inputs(participants, balances, currency, source_type, source_id)
        self._validate_balance_zero_sum(balances, currency)

        creditors, debtors = _build_balance_entries(participants, balances)

        obligations: list[SettlementObligation] = []
        di = 0  # debtor index
        ci = 0  # creditor index

        while ci < len(creditors) and di < len(debtors):
            creditor = creditors[ci]
            debtor = debtors[di]

            matched = min(creditor.amount, debtor.amount)
            matched = _round_for_currency(matched, currency)

            if matched > ZERO:
                obligation_id = _make_obligation_id(source_type, source_id, len(obligations))
                obligations.append(
                    SettlementObligation(
                        obligation_id=obligation_id,
                        creditor_id=creditor.participant_id,
                        debtor_id=debtor.participant_id,
                        amount=matched,
                        currency=currency,
                        source_type=source_type,
                        source_id=source_id,
                    )
                )

            # Advance pointers.
            remaining_creditor = _round_for_currency(creditor.amount - matched, currency)
            remaining_debtor = _round_for_currency(debtor.amount - matched, currency)

            object.__setattr__(creditor, "amount", remaining_creditor)
            object.__setattr__(debtor, "amount", remaining_debtor)

            if remaining_creditor == ZERO:
                ci += 1
            if remaining_debtor == ZERO:
                di += 1

        # Final sort for deterministic output: creditor_id then debtor_id.
        obligations.sort(key=lambda o: (o.creditor_id, o.debtor_id))
        return obligations

    # -- input validation ---------------------------------------------------

    @staticmethod
    def _validate_inputs(
        participants: list[str],
        balances: dict[str, Decimal],
        currency: str,
        source_type: str,
        source_id: str,
    ) -> None:
        if not participants:
            raise ValueError("participants must be a non-empty list")
        if not isinstance(participants, list):
            raise ValueError("participants must be a list")
        if len(set(participants)) != len(participants):
            duplicates = [p for p in participants if participants.count(p) > 1]
            raise ValueError(f"Duplicate participants are not allowed: {sorted(set(duplicates))}")
        if not currency or not isinstance(currency, str):
            raise ValueError("currency is required and must be a non-empty string")
        if not source_type or not isinstance(source_type, str):
            raise ValueError("source_type is required and must be a non-empty string")
        if not source_id or not isinstance(source_id, str):
            raise ValueError("source_id is required and must be a non-empty string")

        for participant in participants:
            if participant not in balances:
                raise ValueError(f"Participant {participant!r} is missing from balances")

        for participant, balance in balances.items():
            if participant not in participants:
                raise ValueError(f"Balance key {participant!r} is not in participants list")
            if not isinstance(balance, Decimal):
                if isinstance(balance, float):
                    raise ValueError(
                        f"Balance for {participant!r} is float ({balance!r}); "
                        f"monetary values must be Decimal"
                    )
                raise ValueError(
                    f"Balance for {participant!r} must be Decimal, got {type(balance).__name__}"
                )
            # Validate finiteness and currency scale.
            try:
                money_decimal(balance, label=f"balance for {participant!r}")
                validate_amount_for_currency(
                    balance,
                    currency,
                    label=f"balance for {participant!r}",
                )
            except MoneyValidationError as exc:
                raise ValueError(str(exc)) from exc

    @staticmethod
    def _validate_balance_zero_sum(balances: dict[str, Decimal], currency: str) -> None:
        total = _round_for_currency(sum(balances.values(), ZERO), currency)
        if total != ZERO:
            raise ValueError(f"Balances must sum to zero (total paid = total shares), got {total}")


def _make_obligation_id(source_type: str, source_id: str, index: int) -> str:
    """Generate a deterministic obligation id.

    Uses UUID5 with a stable namespace for repeatability across runs.
    """
    namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
    name = f"{source_type}:{source_id}:{index}"
    return str(uuid.uuid5(namespace, name))


# Convenience function matching the task spec API.
def generate_obligations(
    participants: list[str],
    balances: dict[str, Decimal],
    *,
    currency: str = "SGD",
    source_type: str = "settlement_runtime",
    source_id: str = "default",
) -> list[SettlementObligation]:
    """Convenience wrapper around SettlementRuntime.generate_obligations()."""
    runtime = SettlementRuntime()
    return runtime.generate_obligations(
        participants=participants,
        balances=balances,
        currency=currency,
        source_type=source_type,
        source_id=source_id,
    )
