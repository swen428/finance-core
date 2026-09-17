"""Calculation audit data models.

Immutable audit snapshot dataclasses with Decimal monetary safety,
validation guards, and timestamp injection for test stability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from finance_core.money import SUPPORTED_CURRENCIES, ZERO, money_decimal

# Valid audit status values.
VALID_AUDIT_STATUSES: frozenset[str] = frozenset(
    {
        "calculated",
        "failed",
        "superseded",
        "pending_confirmation",
    }
)


class AuditValidationError(ValueError):
    """Audit snapshot failed validation and is not a valid audit record."""


@dataclass(frozen=True)
class AuditRoundingEntry:
    """One rounding adjustment recorded for audit traceability."""

    participant: str
    amount: Decimal
    policy: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise AuditValidationError(
                f"Rounding amount for {self.participant!r} must be Decimal, "
                f"got {type(self.amount).__name__}"
            )
        if not self.participant:
            raise AuditValidationError("Rounding participant is required")
        if not self.policy:
            raise AuditValidationError("Rounding policy is required")


@dataclass(frozen=True)
class AuditSettlementEntry:
    """One settlement obligation recorded for audit traceability."""

    debtor: str
    creditor: str
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            raise AuditValidationError(
                f"Settlement amount for {self.debtor}->{self.creditor} must be Decimal, "
                f"got {type(self.amount).__name__}"
            )
        if self.amount <= ZERO:
            raise AuditValidationError(
                f"Settlement amount for {self.debtor}->{self.creditor} "
                f"must be positive, got {self.amount}"
            )
        if self.debtor == self.creditor:
            raise AuditValidationError(
                f"Self-obligation rejected: {self.debtor} owes {self.creditor}"
            )
        if not self.debtor:
            raise AuditValidationError("Settlement debtor is required")
        if not self.creditor:
            raise AuditValidationError("Settlement creditor is required")
        if not self.currency:
            raise AuditValidationError("Settlement currency is required")


def _validate_decimal_dict(
    data: dict[str, Any],
    label: str,
) -> None:
    """Reject float values in a dict that should contain only Decimal-safe values."""
    if not isinstance(data, dict):
        raise AuditValidationError(f"{label} must be a dict, got {type(data).__name__}")
    for key, value in data.items():
        if isinstance(value, float):
            raise AuditValidationError(
                f"{label}[{key!r}] is float ({value!r}); monetary values must not be floats"
            )


@dataclass(frozen=True)
class AuditSnapshot:
    """Immutable audit record for a deterministic calculation run.

    Captures the full calculation context needed for future reconciliation
    explainability without making AI the final authority for financial amounts.
    """

    calculation_run_id: str
    source_type: str
    source_reference: str
    status: str
    currency: str
    input_snapshot: dict[str, Any] = field(default_factory=dict)
    rules_snapshot: dict[str, Any] = field(default_factory=dict)
    output_snapshot: dict[str, Any] = field(default_factory=dict)
    rounding_snapshot: list[dict[str, Any]] = field(default_factory=list)
    settlement_snapshot: dict[str, Any] = field(default_factory=dict)
    evidence_references: tuple[str, ...] = ()
    created_at: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        # Required fields
        if not self.calculation_run_id:
            raise AuditValidationError("calculation_run_id is required")
        if not self.source_type:
            raise AuditValidationError("source_type is required")
        if not self.source_reference:
            raise AuditValidationError("source_reference is required")
        if not self.status:
            raise AuditValidationError("status is required")
        if self.status not in VALID_AUDIT_STATUSES:
            raise AuditValidationError(
                f"Invalid audit status {self.status!r}; "
                f"must be one of {sorted(VALID_AUDIT_STATUSES)}"
            )
        if not self.currency:
            raise AuditValidationError("currency is required")
        if self.currency not in SUPPORTED_CURRENCIES:
            raise AuditValidationError(f"Unsupported audit currency {self.currency!r}")
        if not self.created_at:
            raise AuditValidationError("created_at timestamp is required")

        # Monetary safety: reject float values in all snapshot dicts
        for label, snapshot in [
            ("input_snapshot", self.input_snapshot),
            ("rules_snapshot", self.rules_snapshot),
            ("output_snapshot", self.output_snapshot),
        ]:
            if snapshot:
                _validate_decimal_dict(snapshot, label)

        # Monetary safety: reject float values in settlement snapshot
        settlement = self.settlement_snapshot
        if settlement:
            for key in ("total_to_collect",):
                val = settlement.get(key)
                if isinstance(val, float):
                    raise AuditValidationError(f"settlement_snapshot[{key!r}] is float ({val!r})")
            for i, obl in enumerate(settlement.get("obligations", [])):
                amount = obl.get("amount")
                if isinstance(amount, float):
                    raise AuditValidationError(
                        f"settlement_snapshot.obligations[{i}].amount is float ({amount!r})"
                    )

        # Validate total paid = own share + collectable
        self._validate_total_reconciliation()

        # Validate settlement obligations sum matches total_to_collect
        self._validate_settlement_obligations()

    def _validate_settlement_obligations(self) -> None:
        settlement = self.settlement_snapshot
        obligations = settlement.get("obligations", [])
        if not obligations:
            return

        total_collect_key = "total_to_collect"
        if total_collect_key not in settlement:
            return

        total_collect_d = _to_decimal(settlement[total_collect_key])
        sum_obligations = sum(
            (_to_decimal(o.get("amount", 0)) for o in obligations),
            ZERO,
        )

        if sum_obligations != total_collect_d:
            raise AuditValidationError(
                f"Settlement obligations sum {sum_obligations} "
                f"does not match total_to_collect {total_collect_d}"
            )

    def _validate_total_reconciliation(self) -> None:
        output = self.output_snapshot
        if not output:
            return

        total_paid = output.get("total_paid")
        payer_own_share = output.get("payer_own_share")
        total_to_collect = output.get("total_to_collect")

        if total_paid is None or payer_own_share is None or total_to_collect is None:
            return  # Incomplete output -- validation deferred

        # Convert to Decimal for precise comparison
        total_paid_d = _to_decimal(total_paid)
        payer_own_d = _to_decimal(payer_own_share)
        collect_d = _to_decimal(total_to_collect)

        expected_collect = total_paid_d - payer_own_d
        if collect_d != expected_collect:
            raise AuditValidationError(
                f"Total to collect {collect_d} does not equal "
                f"total_paid {total_paid_d} - payer_own_share {payer_own_d} "
                f"(expected {expected_collect})"
            )


def _to_decimal(value: object) -> Decimal:
    """Convert a value to Decimal, rejecting floats.

    Wraps the shared Money Contract's ``money_decimal`` and re-raises
    as ``AuditValidationError`` to preserve the existing error contract.
    """
    try:
        return money_decimal(value, label="audit monetary value")
    except ValueError as exc:
        raise AuditValidationError(str(exc)) from exc


def to_decimal_safe(value: object) -> Decimal:
    """Public wrapper for Decimal-safe conversion used by audit layer consumers."""
    return _to_decimal(value)
