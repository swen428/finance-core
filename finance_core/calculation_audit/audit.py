"""Deterministic audit snapshot creation and validation.

Creates immutable audit snapshots from receipt finalization results
and validates them against financial correctness rules.  The audit
layer rejects float monetary values, missing required fields, and
unbalanced settlement totals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from finance_core.calculation_audit.models import (
    AuditSnapshot,
    AuditValidationError,
    _to_decimal,
)
from finance_core.money import (
    SUPPORTED_CURRENCIES,
    ZERO,
    MoneyValidationError,
    quantum_for_currency,
    require_same_currency,
)

SOURCE_TYPE = "receipt_finalization"
APPLIED_RULES_VERSION = "receipt_finalization_v1"
CanonicalObligation = tuple[str, str, Decimal, str]


def create_audit_snapshot(
    calc_result: dict[str, Any],
    *,
    calculation_run_id: str,
    source_type: str = SOURCE_TYPE,
    source_reference: str = "",
    status: str = "calculated",
    settlement_obligations: list[dict[str, Any]] | None = None,
    evidence_references: tuple[str, ...] = (),
    now: str | None = None,
) -> AuditSnapshot:
    """Create an immutable audit snapshot from a deterministic calculator result.

    Args:
        calc_result: Full output from calculate_receipt_split.
        calculation_run_id: Stable identifier for this calculation run.
        source_type: Category of the calculation source.
        source_reference: Reference to the source (e.g. receipt group public ID).
        status: Audit status.
        settlement_obligations: Optional list of settlement obligation dicts
            (debtor, creditor, amount, currency).  If not provided, extracted
            from calc_result.
        evidence_references: Links to source evidence.
        now: ISO 8601 timestamp for determinism in tests.  Defaults to UTC now.

    Returns:
        A validated AuditSnapshot.
    """
    currency = _require_str(calc_result, "currency", "calculator output")
    if currency not in SUPPORTED_CURRENCIES:
        raise AuditValidationError(f"Unsupported audit currency {currency!r}")
    _validate_receipt_currencies(calc_result, currency)
    calculator_obligations = calc_result.get("settlement_obligations", [])
    _validate_obligation_currencies(
        calculator_obligations,
        currency,
        "calculator output settlement_obligations",
    )
    if settlement_obligations is not None:
        _validate_obligation_currencies(
            settlement_obligations,
            currency,
            "settlement override",
        )
    now = now or datetime.now(timezone.utc).isoformat()

    # --- input snapshot (case-level inputs) ---
    input_snapshot = _build_input_snapshot(calc_result)

    # --- rules snapshot (business rules applied) ---
    rules_snapshot = _build_rules_snapshot(calc_result, currency)

    # --- output snapshot (deterministic calculator result) ---
    output_snapshot = _build_output_snapshot(calc_result, currency)

    # --- rounding snapshot ---
    rounding_snapshot = _build_rounding_snapshot(calc_result, currency)

    # --- settlement snapshot ---
    if settlement_obligations is None:
        settlement_obligations = calculator_obligations
    settlement_snapshot = _build_settlement_snapshot(
        settlement_obligations,
        calc_result,
        currency,
    )

    snapshot = AuditSnapshot(
        calculation_run_id=calculation_run_id,
        source_type=source_type,
        source_reference=source_reference,
        status=status,
        currency=currency,
        input_snapshot=input_snapshot,
        rules_snapshot=rules_snapshot,
        output_snapshot=output_snapshot,
        rounding_snapshot=rounding_snapshot,
        settlement_snapshot=settlement_snapshot,
        evidence_references=evidence_references,
        created_at=now,
        version=1,
    )
    validate_audit_snapshot(snapshot)
    return snapshot


def validate_audit_snapshot(snapshot: AuditSnapshot) -> None:
    """Validate an audit snapshot for financial correctness and safety.

    Raises AuditValidationError if any guard fails.
    """
    # --- Guard: every recorded currency matches snapshot authority ---
    _validate_snapshot_currencies(snapshot)

    # --- Guard: no float monetary values ---
    _validate_no_float_values(snapshot)

    # --- Guard: Decimal values parse correctly ---
    _validate_decimal_values(snapshot)

    # --- Guard: currency present ---
    if not snapshot.currency:
        raise AuditValidationError("Audit snapshot currency is missing")

    # --- Guard: source reference present ---
    if not snapshot.source_reference:
        raise AuditValidationError("Audit snapshot source_reference is missing")

    # --- Guard: total paid = own share + collectable ---
    _validate_total_reconciliation(snapshot)

    # --- Guard: settlement obligations sum correctly ---
    _validate_settlement_obligations(snapshot)

    # --- Guard: rounding adjustment recorded when non-zero ---
    _validate_rounding_recorded(snapshot)

    # --- Guard: output matches calculation result ---
    _validate_output_consistency(snapshot)


# -- internal builders --


def _build_input_snapshot(calc_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": _safe_str(calc_result.get("case_id")),
        "currency": _safe_str(calc_result.get("currency")),
        "participants": list(calc_result.get("participants", [])),
        "payer": _safe_str(calc_result.get("payer")),
        "receipt_count": len(calc_result.get("receipts", [])),
    }


def _build_rules_snapshot(calc_result: dict[str, Any], currency: str) -> dict[str, Any]:
    rules: dict[str, Any] = {
        "version": APPLIED_RULES_VERSION,
        "rounding_policy": "payer_following",
        "rounding_method": "ROUND_HALF_UP",
        "rounding_quantum": str(quantum_for_currency(currency)),
    }

    discount_methods: set[str] = set()
    service_methods: set[str] = set()
    for receipt in calc_result.get("receipts", []):
        for adj in receipt.get("adjustments", []):
            method = adj.get("method", "")
            if adj.get("type") == "discount" and method:
                discount_methods.add(method)
            elif adj.get("type") == "service_charge" and method:
                service_methods.add(method)

    if discount_methods:
        rules["discount_allocation_methods"] = sorted(discount_methods)
    if service_methods:
        rules["service_charge_allocation_methods"] = sorted(service_methods)

    return rules


def _build_output_snapshot(
    calc_result: dict[str, Any],
    currency: str,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "currency": currency,
        "participant_shares": _decimal_map(
            calc_result.get("participant_shares", {}),
            currency,
        ),
    }

    payer = calc_result.get("payer")
    if payer is not None:
        output["payer"] = payer

    for key in ("payer_own_share", "total_paid_by_payer", "total_to_collect", "total_paid"):
        value = calc_result.get(key)
        if value is not None:
            output[key] = _decimal_str(value, currency)

    obligations = calc_result.get("settlement_obligations", [])
    if obligations:
        output["settlement_obligations"] = [
            _build_obligation_snapshot(
                obligation,
                currency,
                f"calculator output settlement_obligations[{index}]",
            )
            for index, obligation in enumerate(obligations)
        ]

    return output


def _build_rounding_snapshot(
    calc_result: dict[str, Any],
    currency: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for receipt in calc_result.get("receipts", []):
        adj = receipt.get("rounding_adjustment")
        if adj is not None and adj != ZERO:
            entries.append(
                {
                    "receipt_label": _safe_str(receipt.get("merchant") or receipt.get("label")),
                    "participant": _safe_str(receipt.get("rounding_adjustment_participant")),
                    "amount": _decimal_str(adj, currency),
                    "policy": "payer",
                }
            )
    return entries


def _build_settlement_snapshot(
    settlement_obligations: list[dict[str, Any]],
    calc_result: dict[str, Any],
    currency: str,
) -> dict[str, Any]:
    payer = calc_result.get("payer", "")

    total_collect = sum(
        (_to_decimal(o.get("amount", 0)) for o in settlement_obligations),
        ZERO,
    )

    return {
        "payer": payer,
        "currency": currency,
        "obligation_count": len(settlement_obligations),
        "total_to_collect": _decimal_str(total_collect, currency),
        "obligations": [
            _build_obligation_snapshot(
                obligation,
                currency,
                f"settlement snapshot obligation[{index}]",
            )
            for index, obligation in enumerate(settlement_obligations)
        ],
    }


# -- internal validators --


def _validate_no_float_values(snapshot: AuditSnapshot) -> None:
    _check_dict_no_floats("input_snapshot", snapshot.input_snapshot)
    _check_dict_no_floats("rules_snapshot", snapshot.rules_snapshot)
    _check_dict_no_floats("output_snapshot", snapshot.output_snapshot)

    for i, entry in enumerate(snapshot.rounding_snapshot):
        for key, value in entry.items():
            if isinstance(value, float):
                raise AuditValidationError(f"rounding_snapshot[{i}][{key!r}] is float ({value!r})")

    settlement = snapshot.settlement_snapshot
    for key in ("total_to_collect",):
        if key in settlement and isinstance(settlement[key], float):
            raise AuditValidationError(
                f"settlement_snapshot[{key!r}] is float ({settlement[key]!r})"
            )
    for i, obl in enumerate(settlement.get("obligations", [])):
        amount = obl.get("amount")
        if isinstance(amount, float):
            raise AuditValidationError(
                f"settlement_snapshot.obligations[{i}].amount is float ({amount!r})"
            )


def _check_dict_no_floats(label: str, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if isinstance(value, float):
            raise AuditValidationError(f"{label}[{key!r}] is float ({value!r}); Decimal required")
        if isinstance(value, dict):
            _check_dict_no_floats(f"{label}[{key!r}]", value)


def _validate_decimal_values(snapshot: AuditSnapshot) -> None:
    output = snapshot.output_snapshot
    for key in ("payer_own_share", "total_paid_by_payer", "total_to_collect", "total_paid"):
        val = output.get(key)
        if val is not None:
            try:
                _to_decimal(val)
            except (AuditValidationError, ValueError, TypeError) as exc:
                raise AuditValidationError(
                    f"output_snapshot[{key!r}] is not a valid Decimal value: {val!r}"
                ) from exc

    for name, amount in output.get("participant_shares", {}).items():
        try:
            _to_decimal(amount)
        except (AuditValidationError, ValueError, TypeError) as exc:
            raise AuditValidationError(
                f"output_snapshot.participant_shares[{name!r}] "
                f"is not a valid Decimal value: {amount!r}"
            ) from exc


def _validate_total_reconciliation(snapshot: AuditSnapshot) -> None:
    output = snapshot.output_snapshot
    total_paid = output.get("total_paid")
    payer_own = output.get("payer_own_share")
    total_collect = output.get("total_to_collect")

    if total_paid is None or payer_own is None or total_collect is None:
        return  # partial output, skip reconciliation

    total_paid_d = _to_decimal(total_paid)
    payer_own_d = _to_decimal(payer_own)
    collect_d = _to_decimal(total_collect)

    expected = total_paid_d - payer_own_d
    if collect_d != expected:
        raise AuditValidationError(
            f"Total to collect {collect_d} does not equal "
            f"total_paid {total_paid_d} - payer_own_share {payer_own_d} "
            f"(expected {expected})"
        )


def _validate_settlement_obligations(snapshot: AuditSnapshot) -> None:
    settlement = snapshot.settlement_snapshot
    obligations = settlement.get("obligations", [])
    if not obligations:
        return

    sum_obligations = sum(
        (_to_decimal(o.get("amount", 0)) for o in obligations),
        ZERO,
    )
    total_collect = _to_decimal(settlement.get("total_to_collect", 0))

    if sum_obligations != total_collect:
        raise AuditValidationError(
            f"Settlement obligations sum {sum_obligations} "
            f"does not match total_to_collect {total_collect}"
        )


def _validate_rounding_recorded(snapshot: AuditSnapshot) -> None:
    """Guard: if there are non-zero rounding entries, ensure they have required fields."""
    for i, entry in enumerate(snapshot.rounding_snapshot):
        amount = entry.get("amount")
        if amount is not None:
            try:
                amount_d = _to_decimal(amount)
                if amount_d != ZERO and not entry.get("participant"):
                    raise AuditValidationError(
                        f"rounding_snapshot[{i}] has non-zero amount but no participant recorded"
                    )
            except AuditValidationError:
                raise
            except (ValueError, TypeError) as exc:
                raise AuditValidationError(
                    f"rounding_snapshot[{i}].amount invalid: {amount!r}"
                ) from exc


def _validate_output_consistency(snapshot: AuditSnapshot) -> None:
    """Guard: settlement obligations in output match settlement snapshot."""
    output_obligations = snapshot.output_snapshot.get("settlement_obligations", [])
    settlement_obligations = snapshot.settlement_snapshot.get("obligations", [])

    if not output_obligations and not settlement_obligations:
        return

    if len(output_obligations) != len(settlement_obligations):
        raise AuditValidationError(
            f"output_snapshot has {len(output_obligations)} obligations "
            f"but settlement_snapshot has {len(settlement_obligations)}"
        )

    output_canonical = sorted(
        _canonical_obligation(o, f"output_snapshot.settlement_obligations[{i}]")
        for i, o in enumerate(output_obligations)
    )
    settlement_canonical = sorted(
        _canonical_obligation(o, f"settlement_snapshot.obligations[{i}]")
        for i, o in enumerate(settlement_obligations)
    )
    if output_canonical != settlement_canonical:
        raise AuditValidationError(
            "settlement obligations do not match output settlement_obligations"
        )


# -- helpers --


def _decimal_map(shares: dict[str, Any], currency: str) -> dict[str, str]:
    return {name: _decimal_str(amount, currency) for name, amount in shares.items()}


def _decimal_str(value: object, currency: str) -> str:
    d = _to_decimal(value)
    return str(d.quantize(quantum_for_currency(currency), rounding=ROUND_HALF_UP))


def _build_obligation_snapshot(
    obligation: dict[str, Any],
    currency: str,
    label: str,
) -> dict[str, Any]:
    obligation_currency = _require_consistent_audit_currency(
        currency,
        obligation.get("currency"),
        f"{label} currency",
    )
    return {
        "debtor": obligation["debtor"],
        "creditor": obligation["creditor"],
        "amount": _decimal_str(obligation["amount"], obligation_currency),
        "currency": obligation_currency,
    }


def _validate_obligation_currencies(
    obligations: object,
    currency: str,
    label: str,
) -> None:
    if not isinstance(obligations, list):
        raise AuditValidationError(f"{label} must be a list, got {type(obligations).__name__}")
    for index, obligation in enumerate(obligations):
        if not isinstance(obligation, dict):
            raise AuditValidationError(
                f"{label}[{index}] must be a dict, got {type(obligation).__name__}"
            )
        _require_consistent_audit_currency(
            currency,
            obligation.get("currency"),
            f"{label}[{index}] currency",
        )


def _validate_snapshot_currencies(snapshot: AuditSnapshot) -> None:
    currency = _require_consistent_audit_currency(
        snapshot.currency,
        snapshot.currency,
        "Audit snapshot currency",
    )
    for label, section in (
        ("input_snapshot", snapshot.input_snapshot),
        ("output_snapshot", snapshot.output_snapshot),
        ("settlement_snapshot", snapshot.settlement_snapshot),
    ):
        if section:
            _require_consistent_audit_currency(
                currency,
                section.get("currency"),
                f"{label} currency",
            )

    _validate_obligation_currencies(
        snapshot.output_snapshot.get("settlement_obligations", []),
        currency,
        "output snapshot obligation",
    )
    _validate_obligation_currencies(
        snapshot.settlement_snapshot.get("obligations", []),
        currency,
        "settlement snapshot obligation",
    )


def _validate_receipt_currencies(calc_result: dict[str, Any], currency: str) -> None:
    for index, receipt in enumerate(calc_result.get("receipts", [])):
        if not isinstance(receipt, dict):
            raise AuditValidationError(
                f"calculator output receipt[{index}] must be a dict, got {type(receipt).__name__}"
            )
        _require_consistent_audit_currency(
            currency,
            receipt.get("currency"),
            f"calculator output receipt[{index}] currency",
        )

        for collection, component_label in (
            ("items", "item"),
            ("adjustments", "adjustment"),
        ):
            for nested_index, component in enumerate(receipt.get(collection, [])):
                if isinstance(component, dict) and "currency" in component:
                    _require_consistent_audit_currency(
                        currency,
                        component["currency"],
                        f"calculator output receipt[{index}] "
                        f"{component_label}[{nested_index}] currency",
                    )


def _require_consistent_audit_currency(
    currency: str,
    candidate: object,
    label: str,
) -> str:
    if not isinstance(candidate, str):
        raise AuditValidationError(
            f"{label} is invalid: Currency must be a string, got {type(candidate).__name__}"
        )
    try:
        require_same_currency(
            currency,
            candidate,
            label_a="audit currency",
            label_b=label,
        )
    except MoneyValidationError as exc:
        raise AuditValidationError(f"{label} is invalid: {exc}") from exc
    return candidate


def _canonical_obligation(value: Any, label: str) -> CanonicalObligation:
    if not isinstance(value, dict):
        raise AuditValidationError(f"{label} must be a dict")
    try:
        debtor = value["debtor"]
        creditor = value["creditor"]
        amount = value["amount"]
        currency = value["currency"]
    except KeyError as exc:
        raise AuditValidationError(f"{label} missing required field {exc.args[0]!r}") from exc
    if not isinstance(debtor, str) or not debtor:
        raise AuditValidationError(f"{label}.debtor is required")
    if not isinstance(creditor, str) or not creditor:
        raise AuditValidationError(f"{label}.creditor is required")
    if not isinstance(currency, str) or not currency:
        raise AuditValidationError(f"{label}.currency is required")
    return (debtor, creditor, _to_decimal(amount), currency)


def _safe_str(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _require_str(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not value:
        raise AuditValidationError(f"{label} is missing required field {key!r}")
    return str(value)
