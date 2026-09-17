from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
from typing import Any

from finance_core.money import (
    ZERO,
    CurrencyMismatchError,
    MoneyValidationError,
    SignPolicy,
    minor_units,
    money_decimal,
    normalize_currency,
    quantize_for_currency,
    require_same_currency,
    validate_amount_for_currency,
)

ADD_DIRECTIONS = {"add", "charge"}
SUBTRACT_DIRECTIONS = {"subtract", "discount"}

# ---------------------------------------------------------------------------
# Computational epsilon for Stage A exact-reconciliation equality.
# ---------------------------------------------------------------------------
# Proportional allocation with equal splits can produce repeating Decimal
# values (e.g. 10.00 / 3 = 3.333...).  Those values may differ from the
# exact algebraic sum by a tiny fraction — purely an artifact of finite
# Decimal precision, not a genuine financial mismatch.
#
# This epsilon is intentionally MANY orders of magnitude smaller than the
# smallest supported currency quantum (JPY 1 unit):
#
#   1E-18  <  1  (JPY quantum)        by  18 orders of magnitude
#   1E-18  <  0.01  (SGD quantum)     by  16 orders of magnitude
#
# It exists ONLY to absorb repeating-division artifacts.  Real discrepancies
# (10.004 SGD, 99.6 JPY, a missing adjustment, etc.) are far larger and will
# fail the exact-equality check.
# ============================================================================
_STAGE_A_EPSILON = Decimal("1E-18")


# ============================================================================
#  Stage A — exact financial calculation
# ============================================================================
# Before any rounding or minor-unit allocation, exact unrounded share values
# MUST sum to the exact authoritative net_paid.  This proves that:
#
#   * items, subtotal, service charge, discount, adjustments, net_paid,
#     and allocation formulas are financially consistent.
#
# A mismatch at this stage is a business-calculation error — it must never
# be silently absorbed by rounding or assigned to the payer.
# ============================================================================


def _validate_exact_reconciliation(
    exact_shares: dict[str, Decimal],
    exact_total: Decimal,
    *,
    receipt_label: str,
    currency: str,
) -> None:
    """Validate that exact unrounded shares equal the authoritative total.

    Uses strict exact equality with a tiny computational epsilon
    (``_STAGE_A_EPSILON``, currently 1E-18) that exists only to absorb
    repeating-division artifacts from proportional allocation.  Real
    financial discrepancies (10.004 ≠ 10.00 SGD, 99.6 ≠ 100 JPY) are
    many orders of magnitude larger and will fail this check.

    The epsilon is independent of the currency's minor-unit scale — it
    must not be confused with an amount-quantization tolerance.

    Raises ``ValueError`` with the receipt label, exact share total,
    expected total, and difference if they disagree.
    """
    exact_sum = sum(exact_shares.values(), ZERO)
    diff = abs(exact_total - exact_sum)
    if diff > _STAGE_A_EPSILON:
        raise ValueError(
            f"{receipt_label}: exact calculated share total {exact_sum} "
            f"does not equal authoritative net paid {exact_total} "
            f"(|difference|: {diff}). "
            f"This is a calculation error — it must not be hidden by rounding."
        )


# ============================================================================
#  Stage B — currency-scale minor-unit allocation
# ============================================================================
# Only after Stage A passes may exact shares be converted to the currency's
# minor-unit scale.  A legitimate minor-unit remainder caused solely by
# representing exact proportional values at the currency's resolution may
# be distributed deterministically.  The payer-following policy may decide
# *who* receives that remainder, but the remainder is always an integer
# number of currency minor units — never an arbitrary Decimal difference.
# ============================================================================


def _allocate_minor_units(
    exact_shares: dict[str, Decimal],
    exact_total: Decimal,
    currency: str,
    *,
    participants: list[str],
    remainder_policy: str = "largest_remainder",
    payer: str | None = None,
) -> dict[str, Decimal]:
    """Allocate exact shares to currency minor units.

    1. Convert the authoritative total to integer minor units.
    2. Derive each participant's floor (or ROUND_HALF_UP) base allocation
       in minor units, depending on the policy.
    3. Calculate the exact integer number of remaining minor units.
    4. Distribute remaining units according to *remainder_policy*:
       - ``"largest_remainder"``: one unit each to the participants with the
         largest fractional remainders, ties broken by participant public ID
         ascending.
       - ``"payer"``: assign all remaining units to *payer*.
    5. Verify final sum == exact_total at the currency's scale.

    Args:
        exact_shares: Exact (potentially high-precision) share for each
            participant.
        exact_total: The authoritative receipt total (already validated at
            the currency's scale).
        currency: Authoritative currency code.
        participants: Deterministic ordered participant list.
        remainder_policy: ``"largest_remainder"`` or ``"payer"``.
        payer: Required when remainder_policy is ``"payer"``.

    Returns:
        A dict mapping each participant to a currency-scale final share.
        ``sum(result) == exact_total`` is guaranteed.

    Raises:
        ValueError: if inputs are invalid, the remainder is out of bounds,
            or the final allocation does not reconcile.
    """
    _validate_allocator_inputs(
        exact_shares, exact_total, currency, participants, remainder_policy, payer
    )

    units = minor_units(currency)
    factor = 10**units
    quantum = Decimal("0.1") ** units
    total_units = _to_minor_units(exact_total, factor)

    # Compute base minor-unit allocation (floor or ROUND_HALF_UP depending on
    # policy) and track fractional remainders.
    floors: dict[str, int] = {}
    remainders: dict[str, Decimal] = {}
    sum_floors = 0

    for p in participants:
        share = exact_shares.get(p, ZERO)
        share_units = _to_minor_units(share, factor)

        if remainder_policy == "payer":
            # ROUND_HALF_UP quantization for payer-following policy.
            base = int(share_units.to_integral_value(rounding="ROUND_HALF_UP"))
            # Fractional part for tie-breaking (not used for distribution in
            # payer mode, but tracked for consistency checks).
            remainder = share_units - Decimal(base)
        else:
            # Floor for largest-remainder.
            base = int(share_units)
            remainder = share_units - Decimal(base)

        floors[p] = base
        sum_floors += base
        remainders[p] = remainder

    remaining = int(total_units) - sum_floors

    # --- guard: remainder bounds -------------------------------------------
    _validate_remainder_bounds(
        remaining=remaining,
        total_units=int(total_units),
        sum_floors=sum_floors,
        participant_count=len(participants),
        currency=currency,
        policy=remainder_policy,
    )

    # --- distribute remaining minor units ----------------------------------
    if remainder_policy == "payer":
        if payer is None:
            raise ValueError("payer must be provided when remainder_policy is 'payer'")
        if remaining != 0:
            new_payer_units = floors[payer] + remaining
            if new_payer_units < 0:
                raise ValueError(
                    f"Payer {payer!r} has {floors[payer]} minor units; "
                    f"cannot absorb a remainder of {remaining} units "
                    f"without becoming negative. "
                    f"(total={total_units} units, currency={currency})"
                )
            floors[payer] = new_payer_units
    elif remainder_policy == "largest_remainder":
        # Rank by descending fractional remainder, then by participant public ID.
        ranked = sorted(
            remainders.items(),
            key=lambda item: (-item[1], item[0]),
        )
        for i in range(remaining):
            p = ranked[i][0]
            floors[p] += 1
    else:
        raise ValueError(f"Unsupported remainder_policy: {remainder_policy!r}")

    # --- convert back to Decimal and verify exact reconciliation -----------
    result: dict[str, Decimal] = {}
    for p in participants:
        result[p] = (Decimal(floors[p]) / Decimal(factor)).quantize(quantum)

    if sum(result.values(), ZERO) != exact_total:
        raise ValueError(
            f"Minor-unit allocation did not reconcile: "
            f"sum={sum(result.values(), ZERO)}, total={exact_total}, "
            f"currency={currency}, policy={remainder_policy}"
        )

    return result


def _validate_allocator_inputs(
    exact_shares: dict[str, Decimal],
    exact_total: Decimal,
    currency: str,
    participants: list[str],
    remainder_policy: str,
    payer: str | None,
) -> None:
    """Validate inputs to the minor-unit allocator before any arithmetic."""
    if not participants:
        raise ValueError("Allocation requires at least one participant")
    if len(set(participants)) != len(participants):
        raise ValueError(
            f"Duplicate participants are not allowed in allocation: "
            f"{sorted(set(p for p in participants if participants.count(p) > 1))}"
        )
    if not exact_total.is_finite():
        raise MoneyValidationError(f"Allocation total must be finite, got {exact_total!r}")
    if exact_total < ZERO:
        raise MoneyValidationError(f"Allocation total must be non-negative, got {exact_total}")
    # Validate total is at currency scale.
    validate_amount_for_currency(exact_total, currency, label="allocation total")
    # Validate every share is finite and non-negative.
    for p in participants:
        share = exact_shares.get(p, ZERO)
        if not share.is_finite():
            raise MoneyValidationError(f"Share for {p!r} must be finite, got {share!r}")
        if share < ZERO:
            raise MoneyValidationError(f"Share for {p!r} must be non-negative, got {share}")
    if remainder_policy not in ("largest_remainder", "payer"):
        raise ValueError(f"Unsupported remainder_policy: {remainder_policy!r}")
    if remainder_policy == "payer" and payer is None:
        raise ValueError("payer must be provided when remainder_policy is 'payer'")
    if remainder_policy == "payer" and payer not in participants:
        raise ValueError(f"Payer {payer!r} is not in the participant set")


def _validate_remainder_bounds(
    remaining: int,
    total_units: int,
    sum_floors: int,
    participant_count: int,
    currency: str,
    policy: str,
) -> None:
    """Validate that the minor-unit remainder is within mathematically
    defensible bounds for the given allocation algorithm.

    * Largest-remainder: ``0 <= remaining <= participant_count``.
    * Payer (ROUND_HALF_UP base): each participant can deviate by at most
      ±½ unit, so ``|remaining| <= participant_count``.

    Raises ``ValueError`` if the remainder is out of bounds.
    """
    if policy in ("payer",):
        # ROUND_HALF_UP per participant: maximum deviation from true value
        # is 0.5 units per participant.  The sum of N independent half-unit
        # deviations implies |remaining| <= N.
        if abs(remaining) > participant_count:
            raise ValueError(
                f"Minor-unit allocation: payer remainder {remaining} "
                f"exceeds allowed bound ±{participant_count} minor units "
                f"(participant count). "
                f"({total_units} total units, {sum_floors} allocated, "
                f"currency={currency}). "
                f"This is a calculation or input error."
            )
        return

    # Largest-remainder (floor base): each participant drops < 1 unit, so
    # 0 <= remaining <= participant_count.
    if remaining < 0:
        raise ValueError(
            f"Minor-unit allocation: floor sum {sum_floors} exceeds "
            f"total {total_units} minor units "
            f"({Decimal(total_units) / (10 ** minor_units(currency))} "
            f"{currency}). This is a calculation or input error."
        )
    if remaining > participant_count:
        raise ValueError(
            f"Minor-unit allocation: remaining units {remaining} exceeds "
            f"participant count {participant_count}. "
            f"Each participant can receive at most one minor unit from "
            f"largest-remainder distribution. "
            f"({total_units} total units, {sum_floors} allocated, "
            f"{currency})"
        )


# ---------------------------------------------------------------------------
# Authoritative monetary input
# ---------------------------------------------------------------------------


def _authoritative_money(
    value: object,
    currency: str,
    *,
    label: str,
    sign_policy: SignPolicy = SignPolicy.ANY,  # type: ignore[attr-defined]
) -> Decimal:
    """Validate a monetary value before any arithmetic.

    Steps (in order):
    1. Parse safely via ``money_decimal`` (rejects float, bool, None,
       NaN, Infinity, scientific notation, malformed strings).
    2. Validate the amount does not exceed the currency's minor-unit
       scale — sub-minor-unit precision is never silently rounded.
    3. Enforce the domain-specific sign policy.

    Re-raises ``MoneyValidationError`` as ``TypeError`` for float input
    to preserve the existing error contract expected by tests.
    """
    try:
        amount = money_decimal(value, label=label)
    except MoneyValidationError as exc:
        if isinstance(value, float):
            raise TypeError("Currency values must not be floats") from exc
        raise ValueError(str(exc)) from exc
    validate_amount_for_currency(amount, currency, label=label)
    sign_policy.enforce(amount, label=label)
    return amount


def _authoritative_service_charge_rate(value: object, *, label: str) -> Decimal:
    """Validate a dimensionless service-charge ratio before arithmetic.

    Rates share the Money Contract's safe Decimal representation rules but are
    not monetary amounts: currency minor-unit scale does not apply.  Owner's
    approved domain range is zero inclusive and one exclusive.
    """
    try:
        rate = money_decimal(value, label=label)
    except MoneyValidationError as exc:
        if isinstance(value, float):
            raise TypeError("Currency values must not be floats") from exc
        raise ValueError(str(exc)) from exc

    if rate < ZERO:
        raise MoneyValidationError(f"{label} must be non-negative, got {rate}")
    if rate >= Decimal("1.00"):
        raise MoneyValidationError(f"{label} must be less than 1.00, got {rate}")
    return rate


def _quantized_service_charge_from_rate(
    item_total: Decimal,
    rate: Decimal,
    currency: str,
) -> Decimal:
    """Multiply exactly, then perform one explicit currency quantization.

    Decimal multiplication uses the active context.  A precision at least the
    sum of both coefficient lengths guarantees an exact finite product; the
    context is then widened for a possible quantize carry so ambient precision
    cannot cause a hidden first rounding before ``ROUND_HALF_UP``.
    """
    multiplication_precision = max(
        1,
        len(item_total.as_tuple().digits) + len(rate.as_tuple().digits),
    )
    with localcontext(Context(prec=multiplication_precision)) as context:
        exact_product = item_total * rate
        quantized_precision = max(
            1,
            exact_product.adjusted() + minor_units(currency) + 2,
        )
        context.prec = max(context.prec, quantized_precision)
        return _round_for_currency(exact_product, currency)


def _to_minor_units(value: Decimal, factor: int) -> Decimal:
    """Convert a Decimal value to minor-unit scale without rounding."""
    return value * Decimal(factor)


# ============================================================================
# Main calculator
# ============================================================================


def calculate_receipt_split(case_data: dict[str, Any]) -> dict[str, Any]:
    """Calculate deterministic receipt split shares and settlement obligations."""
    if not case_data.get("receipts"):
        raise ValueError("At least one receipt is required")

    participants = _case_participants(case_data)
    _require_no_duplicate_participants(participants)

    currency = _resolve_authoritative_currency(case_data)

    participant_shares = _empty_shares(participants)
    payer_paid_amounts = _empty_shares(participants)
    receipt_results = []

    for receipt in case_data["receipts"]:
        receipt_currency = receipt.get("currency", currency)
        if receipt_currency is None:
            raise ValueError("Receipt currency is required")
        require_same_currency(
            currency,
            receipt_currency,
            label_a="authoritative currency",
            label_b=f"receipt {_receipt_label(receipt)} currency",
        )

        receipt_result = _calculate_receipt(receipt, participants, currency)
        receipt_results.append(receipt_result)

        payer = receipt_result["paid_by"]
        payer_paid_amounts[payer] += receipt_result["net_paid"]
        for participant, amount in receipt_result["participant_shares"].items():
            participant_shares[participant] += amount

    participant_shares = _rounded_share_map(participant_shares, currency)
    payer_paid_amounts = _rounded_share_map(payer_paid_amounts, currency)

    payers = [payer for payer, amount in payer_paid_amounts.items() if amount != ZERO]
    primary_payer = case_data.get("payer")
    if primary_payer is not None:
        _validate_primary_payer(primary_payer, participants, payers)
    elif len(payers) == 1:
        primary_payer = payers[0]

    settlement_obligations = _settlement_obligations(
        participants=participants,
        participant_shares=participant_shares,
        payer_paid_amounts=payer_paid_amounts,
        currency=currency,
    )

    total_paid = _round_for_currency(sum(payer_paid_amounts.values(), ZERO), currency)
    total_shares = _round_for_currency(sum(participant_shares.values(), ZERO), currency)
    if total_shares != total_paid:
        raise ValueError("Total participant shares do not equal total paid amount")

    result: dict[str, Any] = {
        "case_id": case_data.get("case_id"),
        "currency": currency,
        "status": case_data.get("status", "calculated_pending_confirmation"),
        "participants": participants,
        "payer": primary_payer,
        "participant_shares": participant_shares,
        "total_participant_shares": participant_shares,
        "payer_paid_amounts": payer_paid_amounts,
        "payer_own_shares": {payer: participant_shares[payer] for payer in payers},
        "settlement_obligations": settlement_obligations,
        "total_paid": total_paid,
        "receipts": receipt_results,
    }

    participant_display_names = case_data.get("participant_display_names")
    if participant_display_names:
        result["participant_display_names"] = dict(participant_display_names)

    if primary_payer is not None:
        payer_obligations = {
            obligation["debtor"]: obligation["amount"]
            for obligation in settlement_obligations
            if obligation["creditor"] == primary_payer
        }
        result["obligations"] = payer_obligations
        result["payer_own_share"] = participant_shares[primary_payer]
        result["total_to_collect"] = _round_for_currency(
            sum(payer_obligations.values(), ZERO), currency
        )
        result["total_paid_by_payer"] = payer_paid_amounts[primary_payer]
    else:
        result["obligations"] = settlement_obligations

    return result


def _resolve_authoritative_currency(case_data: dict[str, Any]) -> str:
    """Resolve and validate the single authoritative currency for this calculation.

    The top-level ``currency`` field is authoritative. It must be explicitly
    provided; the currency is never inferred from nested receipts or items.
    Every receipt must use the same currency; mixed-currency receipt splits
    are rejected.
    """
    raw = case_data.get("currency")
    if raw is None:
        raise MoneyValidationError(
            "Authoritative top-level currency is required for receipt split calculation. "
            "A receipt currency must never establish authority. "
            "Provide a top-level 'currency' field (e.g. \"SGD\")."
        )
    return normalize_currency(raw)


@dataclass(frozen=True)
class ExactReceiptShares:
    """Stage A result: the exact pre-rounding shares for one receipt.

    Deterministic and pure -- no database access, no rounding remainder
    distribution, no wall clock.  The readiness boundary reuses
    :func:`compute_exact_receipt_shares` so a positive readiness report cannot
    promise a projection the deterministic calculator would reject.
    """

    item_total: Decimal
    net_paid: Decimal
    participant_shares: dict[str, Decimal]
    adjustments: list[dict[str, Any]]
    adjustment_details: list[dict[str, Any]]


def compute_exact_receipt_shares(
    receipt: dict[str, Any],
    all_participants: list[str],
    currency: str,
) -> ExactReceiptShares:
    """Compute and reconcile the exact (pre-rounding) shares for one receipt.

    Applies the item allocation rules, the subtotal check, every adjustment in
    order, the net-paid derivation, the Stage A exact reconciliation, and the
    non-negative exact-share rule.  A subtract adjustment that drives a
    participant's exact share below zero fails here, before any rounding, so the
    same rule is reachable without running the minor-unit allocator.
    """
    item_shares, _item_details = _item_shares(receipt, all_participants, currency)
    item_total = sum(item_shares.values(), ZERO)

    # Validate explicit subtotal against currency scale.
    if "subtotal" in receipt:
        expected_subtotal = _authoritative_money(
            receipt["subtotal"],
            currency,
            label=f"{_receipt_label(receipt)} subtotal",
            sign_policy=SignPolicy.NON_NEGATIVE,  # type: ignore[attr-defined]
        )
        if item_total != expected_subtotal:
            receipt_label = _receipt_label(receipt)
            raise ValueError(
                f"{receipt_label} item total {item_total} does not match "
                f"subtotal {expected_subtotal}"
            )
    else:
        # When no explicit subtotal, the item total IS the subtotal —
        # but it must still be at the currency's scale.
        item_total = _round_for_currency(item_total, currency)

    receipt_adjustments = _receipt_adjustments(receipt, item_total, currency)
    running_shares = dict(item_shares)
    adjustment_details: list[dict[str, Any]] = []

    for adjustment in receipt_adjustments:
        allocations = _adjustment_allocations(
            adjustment=adjustment,
            receipt=receipt,
            item_shares=item_shares,
            current_shares=running_shares,
            participants=all_participants,
            currency=currency,
        )
        direction = adjustment["direction"]
        for participant, amount in allocations.items():
            if direction in ADD_DIRECTIONS:
                running_shares[participant] += amount
            elif direction in SUBTRACT_DIRECTIONS:
                running_shares[participant] -= amount
            else:
                raise ValueError(f"Unsupported adjustment direction: {direction}")

        adjustment_details.append(
            {
                "type": adjustment["type"],
                "direction": direction,
                "method": adjustment["allocation_method"],
                "amount": adjustment["amount"],
                "participant_allocations": allocations,
            }
        )

    net_paid = _receipt_net_paid(receipt, item_total, receipt_adjustments, currency)

    # Prove that items + subtotal + adjustments + allocation formulas are
    # financially consistent BEFORE any rounding or minor-unit allocation.
    # A mismatch here is a calculation error — it must never be absorbed
    # by rounding or assigned to the payer.
    _validate_exact_reconciliation(
        exact_shares=running_shares,
        exact_total=net_paid,
        receipt_label=_receipt_label(receipt),
        currency=currency,
    )
    _require_non_negative_exact_shares(running_shares, all_participants, receipt, currency)

    return ExactReceiptShares(
        item_total=item_total,
        net_paid=net_paid,
        participant_shares=running_shares,
        adjustments=receipt_adjustments,
        adjustment_details=adjustment_details,
    )


def _require_non_negative_exact_shares(
    exact_shares: dict[str, Decimal],
    all_participants: list[str],
    receipt: dict[str, Any],
    currency: str,
) -> None:
    """Reject an exact share below zero before minor-unit allocation.

    ``_validate_allocator_inputs`` already refuses negative shares; applying the
    identical rule at the end of Stage A keeps one implementation while making
    the failure reachable from the pure Stage A path alone.
    """
    for participant in all_participants:
        share = exact_shares.get(participant, ZERO)
        if share < ZERO:
            raise MoneyValidationError(
                f"{_receipt_label(receipt)}: exact share for {participant!r} must be "
                f"non-negative in {currency}, got {share}"
            )


def _calculate_receipt(
    receipt: dict[str, Any],
    all_participants: list[str],
    currency: str,
) -> dict[str, Any]:
    payer = receipt.get("paid_by")
    if payer is None:
        raise ValueError("Receipt payer is required")
    if payer not in all_participants:
        raise ValueError(f"Unknown payer {payer}")

    # Validate receipt-level currency is consistent.
    receipt_currency = receipt.get("currency", currency)
    require_same_currency(
        currency,
        receipt_currency,
        label_a="authoritative currency",
        label_b=f"receipt {_receipt_label(receipt)} currency",
    )

    # Validate all nested monetary data has the same currency.
    _validate_nested_currency_consistency(receipt, currency)

    item_shares, item_details = _item_shares(receipt, all_participants, currency)
    exact = compute_exact_receipt_shares(receipt, all_participants, currency)
    running_shares = exact.participant_shares
    adjustment_details = exact.adjustment_details
    net_paid = exact.net_paid

    # Stage A reconciliation and the non-negative exact-share rule already ran
    # inside ``compute_exact_receipt_shares`` above; there is exactly one
    # implementation of those rules.

    # Stage B: minor-unit allocation.
    # Only after Stage A passes may exact shares be converted to currency
    # minor units.  The legitimate rounding remainder (caused solely by
    # representing exact proportional values at finite currency resolution)
    # is distributed deterministically.
    remainder_policy = receipt.get("rounding_policy", "largest_remainder")
    final_shares = _allocate_minor_units(
        exact_shares=running_shares,
        exact_total=net_paid,
        currency=currency,
        participants=all_participants,
        remainder_policy=remainder_policy,
        payer=payer if remainder_policy == "payer" else None,
    )

    # Record rounding adjustments for audit traceability.
    rounding_adjustments = []
    for participant in all_participants:
        diff = final_shares[participant] - running_shares[participant]
        quantized_diff = _round_for_currency(diff, currency)
        if quantized_diff != ZERO:
            rounding_adjustments.append(
                {
                    "participant": participant,
                    "amount": quantized_diff,
                    "policy": remainder_policy,
                }
            )

    return {
        "receipt_id": receipt.get("receipt_id") or receipt.get("id"),
        "label": receipt.get("label") or receipt.get("merchant"),
        "merchant": receipt.get("merchant") or receipt.get("label"),
        "currency": currency,
        "paid_by": payer,
        "net_paid": net_paid,
        "item_shares": _rounded_share_map(item_shares, currency),
        "items": item_details,
        "adjustments": adjustment_details,
        "participant_shares": final_shares,
        "rounding_adjustments": rounding_adjustments,
        "rounding_adjustment": _round_for_currency(
            sum((a["amount"] for a in rounding_adjustments), ZERO), currency
        ),
        "rounding_adjustment_participant": payer if rounding_adjustments else None,
    }


def _validate_nested_currency_consistency(
    receipt: dict[str, Any],
    authoritative_currency: str,
) -> None:
    """Validate that every nested monetary component belongs to the
    authoritative receipt currency."""
    rlabel = _receipt_label(receipt)

    for i, item in enumerate(receipt.get("items", [])):
        item_currency = item.get("currency")
        if item_currency is not None:
            require_same_currency(
                authoritative_currency,
                item_currency,
                label_a="authoritative currency",
                label_b=f"{rlabel} item[{i}] currency",
            )

    for i, adj in enumerate(receipt.get("adjustments", [])):
        adj_currency = adj.get("currency")
        if adj_currency is not None:
            require_same_currency(
                authoritative_currency,
                adj_currency,
                label_a="authoritative currency",
                label_b=f"{rlabel} adjustment[{i}] ({adj.get('type', 'unknown')}) currency",
            )


def _item_shares(
    receipt: dict[str, Any],
    all_participants: list[str],
    currency: str,
) -> tuple[dict[str, Decimal], list[dict[str, Any]]]:
    shares = _empty_shares(all_participants)
    item_details = []

    for item in receipt["items"]:
        amount = _authoritative_money(
            item.get("amount", item.get("line_amount")),
            currency,
            label=f"{_receipt_label(receipt)} item amount",
            sign_policy=SignPolicy.NON_NEGATIVE,  # type: ignore[attr-defined]
        )
        allocation_method = item.get("allocation_method", "equal")
        allocations = _item_allocations(
            item=item,
            amount=amount,
            allocation_method=allocation_method,
            all_participants=all_participants,
            receipt_label=_receipt_label(receipt),
            currency=currency,
        )
        for participant, allocation in allocations.items():
            shares[participant] += allocation

        item_details.append(
            {
                "description": item.get("description") or item.get("name") or item.get("item_name"),
                "amount": amount,
                "allocation_method": allocation_method,
                "participant_allocations": allocations,
            }
        )

    return shares, item_details


def _item_allocations(
    item: dict[str, Any],
    amount: Decimal,
    allocation_method: str,
    all_participants: list[str],
    receipt_label: str,
    currency: str,
) -> dict[str, Decimal]:
    allocations = _empty_shares(all_participants)

    if allocation_method in {"equal", "equal_among_consumers"}:
        item_participants = _item_participants(item)
        if not item_participants:
            raise ValueError(f"{receipt_label} item has no participants")
        _require_no_duplicates_in_list(item_participants, f"{receipt_label} item participants")
        split_amount = amount / Decimal(len(item_participants))
        for participant in item_participants:
            _require_participant(participant, allocations)
            allocations[participant] += split_amount
        return allocations

    if allocation_method == "manual":
        manual_allocations = item.get("allocations")
        if not manual_allocations:
            raise ValueError(f"{receipt_label} manual item allocation is missing allocations")
        for participant, allocation in manual_allocations.items():
            _require_participant(participant, allocations)
            allocations[participant] = _authoritative_money(
                allocation,
                currency,
                label=f"manual item allocation for {participant!r}",
                sign_policy=SignPolicy.NON_NEGATIVE,  # type: ignore[attr-defined]
            )
        if sum(allocations.values(), ZERO) != amount:
            raise ValueError(f"{receipt_label} manual item allocations do not sum to item amount")
        return allocations

    raise ValueError(f"Unsupported item allocation method: {allocation_method}")


def _receipt_adjustments(
    receipt: dict[str, Any],
    item_total: Decimal,
    currency: str,
) -> list[dict[str, Any]]:
    adjustments = []

    service_amount = _service_charge_amount(receipt, item_total, currency)
    service_allocation_method = receipt.get(
        "service_charge_allocation_method",
        "proportional_by_item_amount",
    )
    if service_amount != ZERO or service_allocation_method == "manual":
        adjustments.append(
            {
                "type": "service_charge",
                "direction": "add",
                "allocation_method": service_allocation_method,
                "amount": service_amount,
                "allocations": receipt.get("service_charge_allocations"),
                "participants": receipt.get("service_charge_participants"),
            }
        )

    for adjustment in receipt.get("adjustments", []):
        adjustments.append(_normalize_adjustment(adjustment, currency))

    discount = _authoritative_money(
        receipt.get("discount", ZERO),
        currency,
        label=f"{_receipt_label(receipt)} discount",
        sign_policy=SignPolicy.NON_NEGATIVE,  # type: ignore[attr-defined]
    )
    discount_allocation_method = receipt.get("discount_allocation_method")
    if discount != ZERO or discount_allocation_method == "manual":
        adjustments.append(
            {
                "type": "discount",
                "direction": "subtract",
                "allocation_method": discount_allocation_method,
                "amount": discount,
                "allocations": receipt.get("discount_allocations"),
                "participants": receipt.get("discount_participants"),
            }
        )

    return adjustments


def _normalize_adjustment(
    adjustment: dict[str, Any],
    currency: str,
) -> dict[str, Any]:
    adjustment_type = adjustment.get("type") or adjustment.get("adjustment_type")
    if adjustment_type is None:
        raise ValueError("Adjustment type is required")

    direction = adjustment.get("direction")
    if direction is None:
        direction = "subtract" if adjustment_type in {"discount", "voucher", "promotion"} else "add"

    allocation_method = adjustment.get("allocation_method")
    if allocation_method is None:
        raise ValueError(f"{adjustment_type} allocation method is required")

    return {
        "type": adjustment_type,
        "direction": direction,
        "allocation_method": allocation_method,
        "amount": _authoritative_money(
            adjustment["amount"],
            currency,
            label=f"{adjustment_type} amount",
            sign_policy=SignPolicy.NON_NEGATIVE,  # type: ignore[attr-defined]
        ),
        "allocations": adjustment.get("allocations"),
        "participants": adjustment.get("participants"),
    }


def _adjustment_allocations(
    adjustment: dict[str, Any],
    receipt: dict[str, Any],
    item_shares: dict[str, Decimal],
    current_shares: dict[str, Decimal],
    participants: list[str],
    currency: str,
) -> dict[str, Decimal]:
    allocations = _empty_shares(participants)
    amount = adjustment["amount"]
    method = adjustment["allocation_method"]

    if method == "manual":
        manual_allocations = adjustment.get("allocations")
        if not manual_allocations:
            raise ValueError(f"{adjustment['type']} manual allocation is missing allocations")
        for participant, allocation in manual_allocations.items():
            _require_participant(participant, allocations)
            allocations[participant] = _authoritative_money(
                allocation,
                currency,
                label=f"{adjustment['type']} manual allocation for {participant!r}",
                sign_policy=SignPolicy.NON_NEGATIVE,  # type: ignore[attr-defined]
            )
        if sum(allocations.values(), ZERO) != amount:
            raise ValueError(
                f"{adjustment['type']} manual allocations do not sum to adjustment amount"
            )
        return allocations

    if amount == ZERO:
        return allocations
    if method is None:
        raise ValueError(f"{adjustment['type']} allocation method is required")

    if method == "payer_only":
        allocations[receipt["paid_by"]] = amount
        return allocations

    if method == "equal_per_participant":
        active_participants = _active_adjustment_participants(
            adjustment=adjustment,
            current_shares=current_shares,
            participants=participants,
        )
        _require_no_duplicates_in_list(
            active_participants,
            f"{adjustment['type']} adjustment participants",
        )
        per_participant = amount / Decimal(len(active_participants))
        for participant in active_participants:
            allocations[participant] = per_participant
        return allocations

    if method == "proportional_by_item_amount":
        return _proportional_allocations(amount, item_shares)

    if method == "proportional_by_net_amount":
        return _proportional_allocations(amount, current_shares)

    raise ValueError(f"Unsupported adjustment allocation method: {method}")


def _proportional_allocations(
    amount: Decimal,
    base_shares: dict[str, Decimal],
) -> dict[str, Decimal]:
    allocations = _empty_shares(list(base_shares.keys()))
    base_total = sum(base_shares.values(), ZERO)
    if base_total == ZERO:
        raise ValueError("Proportional allocation requires a non-zero base total")

    for participant, share in base_shares.items():
        if share != ZERO:
            allocations[participant] = amount * share / base_total
    return allocations


def _active_adjustment_participants(
    adjustment: dict[str, Any],
    current_shares: dict[str, Decimal],
    participants: list[str],
) -> list[str]:
    explicit_participants = adjustment.get("participants")
    if explicit_participants:
        for participant in explicit_participants:
            _require_participant(participant, current_shares)
        return list(explicit_participants)

    active_participants = [p for p in participants if current_shares[p] != ZERO]
    if not active_participants:
        raise ValueError("Equal allocation requires at least one active participant")
    return active_participants


def _settlement_obligations(
    participants: list[str],
    participant_shares: dict[str, Decimal],
    payer_paid_amounts: dict[str, Decimal],
    currency: str,
) -> list[dict[str, Any]]:
    debtors: list[dict[str, Any]] = []
    creditors: list[dict[str, Any]] = []
    for participant in participants:
        balance = _round_for_currency(
            payer_paid_amounts[participant] - participant_shares[participant],
            currency,
        )
        if balance < ZERO:
            debtors.append({"participant": participant, "amount": -balance})
        elif balance > ZERO:
            creditors.append({"participant": participant, "amount": balance})

    # Deterministic sort: amount descending, participant public ID ascending.
    debtors.sort(key=lambda e: (-e["amount"], e["participant"]))
    creditors.sort(key=lambda e: (-e["amount"], e["participant"]))

    obligations = []
    debtor_index = 0
    creditor_index = 0
    while debtor_index < len(debtors) and creditor_index < len(creditors):
        debtor = debtors[debtor_index]
        creditor = creditors[creditor_index]
        amount = min(debtor["amount"], creditor["amount"])
        if amount != ZERO:
            obligations.append(
                {
                    "debtor": debtor["participant"],
                    "creditor": creditor["participant"],
                    "amount": _round_for_currency(amount, currency),
                    "currency": currency,
                }
            )

        debtor["amount"] = _round_for_currency(debtor["amount"] - amount, currency)
        creditor["amount"] = _round_for_currency(creditor["amount"] - amount, currency)
        if debtor["amount"] == ZERO:
            debtor_index += 1
        if creditor["amount"] == ZERO:
            creditor_index += 1

    if debtor_index != len(debtors) or creditor_index != len(creditors):
        raise ValueError("Settlement obligations do not balance")

    for i, obl in enumerate(obligations):
        if obl.get("currency", currency) != currency:
            raise CurrencyMismatchError(
                f"settlement_obligations[{i}] currency {obl['currency']!r} "
                f"does not match authoritative currency {currency!r}"
            )

    return obligations


def _receipt_net_paid(
    receipt: dict[str, Any],
    item_total: Decimal,
    adjustments: list[dict[str, Any]],
    currency: str,
) -> Decimal:
    if "net_paid" in receipt:
        return _authoritative_money(
            receipt["net_paid"],
            currency,
            label=f"{_receipt_label(receipt)} net_paid",
            sign_policy=SignPolicy.NON_NEGATIVE,  # type: ignore[attr-defined]
        )

    net_paid = item_total
    for adjustment in adjustments:
        if adjustment["direction"] in ADD_DIRECTIONS:
            net_paid += adjustment["amount"]
        elif adjustment["direction"] in SUBTRACT_DIRECTIONS:
            net_paid -= adjustment["amount"]
    return _round_for_currency(net_paid, currency)


def _service_charge_amount(
    receipt: dict[str, Any],
    item_total: Decimal,
    currency: str,
) -> Decimal:
    if "service_charge_amount" in receipt:
        return _authoritative_money(
            receipt.get("service_charge_amount", ZERO),
            currency,
            label=f"{_receipt_label(receipt)} service charge",
            sign_policy=SignPolicy.NON_NEGATIVE,  # type: ignore[attr-defined]
        )
    if "service_charge_rate" in receipt:
        rate = _authoritative_service_charge_rate(
            receipt["service_charge_rate"],
            label=f"{_receipt_label(receipt)} service charge rate",
        )
        return _quantized_service_charge_from_rate(item_total, rate, currency)
    return ZERO


def _case_participants(case_data: dict[str, Any]) -> list[str]:
    participants = list(case_data.get("participants", []))
    if not participants:
        for receipt in case_data["receipts"]:
            _append_unique(participants, receipt.get("paid_by", case_data.get("payer")))
            for item in receipt.get("items", []):
                for participant in _item_participants(item):
                    _append_unique(participants, participant)
                for participant in (item.get("allocations") or {}).keys():
                    _append_unique(participants, participant)
            for adjustment in receipt.get("adjustments", []):
                for participant in (adjustment.get("allocations") or {}).keys():
                    _append_unique(participants, participant)
            for participant in (receipt.get("discount_allocations") or {}).keys():
                _append_unique(participants, participant)

    if not participants:
        raise ValueError("At least one participant is required")
    return participants


def _require_no_duplicate_participants(participants: list[str]) -> None:
    _require_no_duplicates_in_list(participants, "participant public IDs")


def _require_no_duplicates_in_list(values: list[str], context: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for v in values:
        if v in seen:
            duplicates.add(v)
        seen.add(v)
    if duplicates:
        raise ValueError(f"Duplicate {context} are not allowed: {sorted(duplicates)}")


def _validate_primary_payer(
    primary_payer: str,
    participants: list[str],
    actual_payers: list[str],
) -> None:
    if not primary_payer or not isinstance(primary_payer, str):
        raise ValueError(f"Payer must be a non-empty participant public ID, got {primary_payer!r}")
    if primary_payer not in participants:
        raise ValueError(f"Payer {primary_payer!r} is not in the participant set")
    if primary_payer not in actual_payers:
        raise ValueError(
            f"Payer {primary_payer!r} did not pay any receipt; "
            f"actual payers: {sorted(actual_payers)}"
        )
    if len(actual_payers) == 1 and primary_payer != actual_payers[0]:
        raise ValueError(
            f"Payer {primary_payer!r} does not match the sole actual payer {actual_payers[0]!r}"
        )


def _item_participants(item: dict[str, Any]) -> list[str]:
    return list(item.get("participants") or item.get("consumers") or item.get("owners") or [])


def _append_unique(values: list[str], value: str | None) -> None:
    if value is not None and value not in values:
        values.append(value)


def _receipt_label(receipt: dict[str, Any]) -> str:
    return str(
        receipt.get("merchant") or receipt.get("label") or receipt.get("receipt_id") or "Receipt"
    )


def _require_participant(participant: str, shares: dict[str, Decimal]) -> None:
    if participant not in shares:
        raise ValueError(f"Unknown participant {participant}")


def _empty_shares(participants: list[str]) -> dict[str, Decimal]:
    return {participant: ZERO for participant in participants}


def _rounded_share_map(shares: dict[str, Decimal], currency: str) -> dict[str, Decimal]:
    return {
        participant: _round_for_currency(amount, currency) for participant, amount in shares.items()
    }


def _money(value: Any) -> Decimal:
    """Convert a value to Decimal using the shared Money Contract.

    Re-raises ``MoneyValidationError`` as ``TypeError`` to preserve
    the existing error contract expected by tests.
    """
    try:
        return money_decimal(value, label="monetary value")
    except MoneyValidationError as exc:
        if isinstance(value, float):
            raise TypeError("Currency values must not be floats") from exc
        raise ValueError(str(exc)) from exc


def _round_for_currency(value: Decimal, currency: str) -> Decimal:
    """Quantize *value* to the currency's minor-unit scale using ROUND_HALF_UP."""
    return quantize_for_currency(value, currency)
