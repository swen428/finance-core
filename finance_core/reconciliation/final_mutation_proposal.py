"""Reconciliation Final Mutation Proposal Guard v1 -- bounded dry-run preview
layer that consumes reconciliation apply proposal payloads and produces
deterministic guard decisions and dry-run previews, without writing final
financial records.

This layer is intentionally **read-only**. It validates and previews what
a future final mutation step could do, but never executes SQL INSERT/UPDATE,
writes files, generates settlements, or mutates live data.

Key invariants:
- No final financial record mutation.
- Deterministic guard decisions: same input always produces same output.
- Type-guarded input boundary: only ``FinalMutationProposal`` objects accepted.
- Explicit evidence ref requirements where applicable.
- Stable reason codes for blocked proposals.
- ``is_dry_run=True`` on every preview.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from finance_core.reconciliation.final_mutation_money import (
    CreateMoneyValidationError,
    CreateMoneyValidationIssue,
    validate_reconciliation_create_money,
)

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class FinalMutationAction(str, Enum):
    """Supported final mutation proposal actions for v1.

    ``BLOCKED`` is not a caller-requested action; it is the guard's
    output when a proposal fails validation.
    """

    CREATE_FINAL_TRANSACTION = "create_final_transaction_proposal"
    ADJUST_FINAL_TRANSACTION = "adjust_final_transaction_proposal"
    NO_FINAL_MUTATION = "no_final_mutation"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Blocked reason codes
# ---------------------------------------------------------------------------


class FinalMutationBlockedReason(str, Enum):
    """Stable reason codes emitted by the guard when a proposal is blocked.

    These codes are deterministic and audit-friendly. They never change
    between guard versions for the same input condition.
    """

    UNSUPPORTED_ACTION = "unsupported_action"
    MISSING_AMOUNT = "missing_amount"
    MISSING_CURRENCY = "missing_currency"
    MISSING_DATE = "missing_date"
    MISSING_MERCHANT = "missing_merchant"
    MISSING_SOURCE_REFERENCE = "missing_source_reference"
    MISSING_EVIDENCE_REFS = "missing_evidence_refs"
    CREATE_WITHOUT_SOURCE = "create_without_source"
    ADJUST_WITHOUT_TARGET = "adjust_without_target"
    MISSING_SUGGESTED_FIELDS = "missing_suggested_fields"
    INVALID_PROPOSAL_TYPE = "invalid_proposal_type"
    DIRECT_MUTATION_DETECTED = "direct_mutation_detected"
    INVALID_AMOUNT = "invalid_amount"
    INVALID_CURRENCY = "invalid_currency"


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalMutationProposal:
    """A structured intent to mutate final financial records.

    All monetary fields use ``Decimal``.  Dates use ``date | None``.
    This is a proposal only -- it never writes final records.
    """

    proposal_id: str
    action: FinalMutationAction
    apply_result_ref: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    merchant: str | None = None
    transaction_date: date | None = None
    target_transaction_id: str | None = None
    suggested_fields: dict[str, Any] = field(default_factory=dict)
    source_statement_ref: str | None = None
    source_app_transaction_ref: str | None = None
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""


@dataclass(frozen=True)
class FinalMutationPreview:
    """Dry-run summary of what a future final mutation would produce.

    Every field that was present on the proposal is included here.
    Monetary amounts are stored as strings (not floats) for precision.
    Dates are ISO-format strings.

    ``is_dry_run`` is always ``True`` -- this preview never represents
    a completed write.
    """

    proposal_id: str
    action: FinalMutationAction
    amount: str | None = None
    currency: str | None = None
    merchant: str | None = None
    transaction_date: str | None = None
    target_transaction_id: str | None = None
    suggested_fields: dict[str, Any] = field(default_factory=dict)
    source_statement_ref: str | None = None
    source_app_transaction_ref: str | None = None
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    is_dry_run: bool = True
    preview_note: str = "Dry-run preview only. No final financial records were created or modified."


@dataclass(frozen=True)
class FinalMutationGuardDecision:
    """The guard's deterministic output after evaluating a proposal.

    When ``approved`` is ``True``, ``preview`` contains the dry-run
    preview.  When ``approved`` is ``False``, ``preview`` is ``None``
    and ``blocked_reasons`` lists every reason the proposal was blocked.
    """

    proposal_id: str
    approved: bool
    action: FinalMutationAction
    blocked_reasons: tuple[str, ...] = field(default_factory=tuple)
    preview: FinalMutationPreview | None = None
    guard_version: str = "v1"


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

# Actions the guard accepts as valid caller-requested actions.
_SUPPORTED_CALLER_ACTIONS: frozenset[FinalMutationAction] = frozenset(
    {
        FinalMutationAction.CREATE_FINAL_TRANSACTION,
        FinalMutationAction.ADJUST_FINAL_TRANSACTION,
        FinalMutationAction.NO_FINAL_MUTATION,
    }
)

# Keywords that trigger the direct-mutation-detection guard.
_DIRECT_MUTATION_KEYWORDS: frozenset[str] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE TABLE",
        "statement_transactions",
        "final_transactions",
        "settlement_obligations",
    }
)


class FinalMutationGuard:
    """Deterministic, read-only guard for final mutation proposals.

    Usage::

        guard = FinalMutationGuard()
        decision = guard.evaluate(proposal)
        if decision.approved:
            print(decision.preview.amount)

    The guard never writes final records, opens databases, or calls
    external services.  It is a pure validation + preview layer.
    """

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def evaluate(self, proposal: object) -> FinalMutationGuardDecision:
        """Evaluate a proposal and return a guard decision.

        Only ``FinalMutationProposal`` objects are accepted.  Raw
        dicts, strings, and other unvalidated inputs are blocked.
        """
        # -- Rule 0: type guard --
        if not isinstance(proposal, FinalMutationProposal):
            return self._block(
                proposal_id=_extract_proposal_id(proposal),
                reasons=(FinalMutationBlockedReason.INVALID_PROPOSAL_TYPE.value,),
                action=FinalMutationAction.BLOCKED,
            )

        # -- Rule 1: action gate --
        action_result = self._check_action(proposal)
        if action_result is not None:
            return action_result

        # -- Rule 2: direct mutation detection --
        mutation_result = self._check_direct_mutation(proposal)
        if mutation_result is not None:
            return mutation_result

        # -- Rule 3: required field validation --
        field_result = self._check_required_fields(proposal)
        if field_result is not None:
            return field_result

        # -- All checks passed: build approved decision with preview --
        return self._approve(proposal)

    # ------------------------------------------------------------------
    # Preview builder
    # ------------------------------------------------------------------

    def preview(self, proposal: FinalMutationProposal) -> FinalMutationPreview:
        """Build a deterministic dry-run preview from an approved proposal.

        CREATE money is revalidated through the shared Money Contract so the
        preview contains the exact canonical amount and normalized currency
        that the final-write workflow would use. ADJUST behavior is unchanged.
        """
        amount = str(proposal.amount) if proposal.amount is not None else None
        currency = proposal.currency
        if proposal.action == FinalMutationAction.CREATE_FINAL_TRANSACTION:
            validated_money = validate_reconciliation_create_money(
                proposal.amount,
                proposal.currency,
            )
            amount = validated_money.canonical_amount
            currency = validated_money.currency
        return FinalMutationPreview(
            proposal_id=proposal.proposal_id,
            action=proposal.action,
            amount=amount,
            currency=currency,
            merchant=proposal.merchant,
            transaction_date=(
                proposal.transaction_date.isoformat()
                if proposal.transaction_date is not None
                else None
            ),
            target_transaction_id=proposal.target_transaction_id,
            suggested_fields=dict(proposal.suggested_fields),
            source_statement_ref=proposal.source_statement_ref,
            source_app_transaction_ref=proposal.source_app_transaction_ref,
            evidence_refs=proposal.evidence_refs,
            is_dry_run=True,
            preview_note=(
                "Dry-run preview only. No final financial records were created or modified."
            ),
        )

    # ------------------------------------------------------------------
    # Internal check methods
    # ------------------------------------------------------------------

    @staticmethod
    def _check_action(proposal: FinalMutationProposal) -> FinalMutationGuardDecision | None:
        """Block unsupported actions.  BLOCKED is never a valid caller action."""
        if proposal.action == FinalMutationAction.BLOCKED:
            return FinalMutationGuardDecision(
                proposal_id=proposal.proposal_id,
                approved=False,
                action=FinalMutationAction.BLOCKED,
                blocked_reasons=(FinalMutationBlockedReason.UNSUPPORTED_ACTION.value,),
                preview=None,
            )
        if proposal.action not in _SUPPORTED_CALLER_ACTIONS:
            return FinalMutationGuardDecision(
                proposal_id=proposal.proposal_id,
                approved=False,
                action=FinalMutationAction.BLOCKED,
                blocked_reasons=(FinalMutationBlockedReason.UNSUPPORTED_ACTION.value,),
                preview=None,
            )
        return None

    @staticmethod
    def _check_direct_mutation(
        proposal: FinalMutationProposal,
    ) -> FinalMutationGuardDecision | None:
        """Block proposals whose note or suggested_fields contain
        keywords suggesting direct mutation intent."""
        combined = proposal.note + " " + str(proposal.suggested_fields)
        combined_upper = combined.upper()
        for keyword in _DIRECT_MUTATION_KEYWORDS:
            if keyword.upper() in combined_upper:
                return FinalMutationGuardDecision(
                    proposal_id=proposal.proposal_id,
                    approved=False,
                    action=FinalMutationAction.BLOCKED,
                    blocked_reasons=(FinalMutationBlockedReason.DIRECT_MUTATION_DETECTED.value,),
                    preview=None,
                )
        return None

    @staticmethod
    def _check_required_fields(
        proposal: FinalMutationProposal,
    ) -> FinalMutationGuardDecision | None:
        """Validate that the proposal has all required fields for its action."""
        if proposal.action == FinalMutationAction.CREATE_FINAL_TRANSACTION:
            return _validate_create_fields(proposal)
        if proposal.action == FinalMutationAction.ADJUST_FINAL_TRANSACTION:
            return _validate_adjust_fields(proposal)
        # NO_FINAL_MUTATION: no required fields
        return None

    # ------------------------------------------------------------------
    # Internal builders
    # ------------------------------------------------------------------

    def _approve(self, proposal: FinalMutationProposal) -> FinalMutationGuardDecision:
        """Build an approved guard decision with a dry-run preview."""
        return FinalMutationGuardDecision(
            proposal_id=proposal.proposal_id,
            approved=True,
            action=proposal.action,
            blocked_reasons=(),
            preview=self.preview(proposal),
        )

    @staticmethod
    def _block(
        proposal_id: str,
        reasons: tuple[str, ...],
        action: FinalMutationAction = FinalMutationAction.BLOCKED,
    ) -> FinalMutationGuardDecision:
        """Build a blocked guard decision."""
        return FinalMutationGuardDecision(
            proposal_id=proposal_id,
            approved=False,
            action=action,
            blocked_reasons=reasons,
            preview=None,
        )


# ---------------------------------------------------------------------------
# Per-action field validators
# ---------------------------------------------------------------------------


def _validate_create_fields(
    proposal: FinalMutationProposal,
) -> FinalMutationGuardDecision | None:
    """Validate required fields for CREATE_FINAL_TRANSACTION."""
    reasons: list[str] = []

    amount_missing = proposal.amount is None
    currency_missing = proposal.currency is None or (
        type(proposal.currency) is str and not proposal.currency
    )
    money_issues: frozenset[CreateMoneyValidationIssue] = frozenset()
    try:
        validate_reconciliation_create_money(proposal.amount, proposal.currency)
    except CreateMoneyValidationError as exc:
        money_issues = frozenset(exc.issues)

    if amount_missing:
        reasons.append(FinalMutationBlockedReason.MISSING_AMOUNT.value)
    elif CreateMoneyValidationIssue.AMOUNT in money_issues:
        reasons.append(FinalMutationBlockedReason.INVALID_AMOUNT.value)
    if currency_missing:
        reasons.append(FinalMutationBlockedReason.MISSING_CURRENCY.value)
    elif CreateMoneyValidationIssue.CURRENCY in money_issues:
        reasons.append(FinalMutationBlockedReason.INVALID_CURRENCY.value)
    if proposal.transaction_date is None:
        reasons.append(FinalMutationBlockedReason.MISSING_DATE.value)
    if not proposal.merchant:
        reasons.append(FinalMutationBlockedReason.MISSING_MERCHANT.value)
    if not proposal.source_statement_ref:
        reasons.append(FinalMutationBlockedReason.CREATE_WITHOUT_SOURCE.value)
    if not proposal.evidence_refs:
        reasons.append(FinalMutationBlockedReason.MISSING_EVIDENCE_REFS.value)

    if reasons:
        return FinalMutationGuardDecision(
            proposal_id=proposal.proposal_id,
            approved=False,
            action=FinalMutationAction.BLOCKED,
            blocked_reasons=tuple(reasons),
            preview=None,
        )
    return None


def _validate_adjust_fields(
    proposal: FinalMutationProposal,
) -> FinalMutationGuardDecision | None:
    """Validate required fields for ADJUST_FINAL_TRANSACTION."""
    reasons: list[str] = []

    if not proposal.target_transaction_id:
        reasons.append(FinalMutationBlockedReason.ADJUST_WITHOUT_TARGET.value)
    if not proposal.suggested_fields:
        reasons.append(FinalMutationBlockedReason.MISSING_SUGGESTED_FIELDS.value)
    if not proposal.source_app_transaction_ref:
        reasons.append(FinalMutationBlockedReason.MISSING_SOURCE_REFERENCE.value)
    if not proposal.evidence_refs:
        reasons.append(FinalMutationBlockedReason.MISSING_EVIDENCE_REFS.value)

    if reasons:
        return FinalMutationGuardDecision(
            proposal_id=proposal.proposal_id,
            approved=False,
            action=FinalMutationAction.BLOCKED,
            blocked_reasons=tuple(reasons),
            preview=None,
        )
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_proposal_id(obj: object) -> str:
    """Best-effort proposal_id extraction from non-proposal inputs."""
    if isinstance(obj, dict):
        return str(obj.get("proposal_id", "unknown"))
    if hasattr(obj, "proposal_id"):
        return str(getattr(obj, "proposal_id"))
    return "unknown"


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

_DEFAULT_GUARD: FinalMutationGuard | None = None


def _get_guard() -> FinalMutationGuard:
    """Return a module-level guard instance (lazy-init)."""
    global _DEFAULT_GUARD
    if _DEFAULT_GUARD is None:
        _DEFAULT_GUARD = FinalMutationGuard()
    return _DEFAULT_GUARD


def guard_final_mutation_proposal(
    proposal: FinalMutationProposal,
) -> FinalMutationGuardDecision:
    """Convenience: evaluate a proposal through the default guard.

    This is the recommended entry point for most callers.  For testing
    or custom guard instances, use ``FinalMutationGuard().evaluate()``
    directly.
    """
    return _get_guard().evaluate(proposal)


def preview_final_mutation(
    proposal: FinalMutationProposal,
) -> FinalMutationPreview:
    """Convenience: build a dry-run preview from an approved proposal.

    Does not re-validate.  The caller must ensure the proposal passed
    the guard before calling this function.
    """
    return _get_guard().preview(proposal)


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------

__all__ = [
    "FinalMutationAction",
    "FinalMutationBlockedReason",
    "FinalMutationGuard",
    "FinalMutationGuardDecision",
    "FinalMutationPreview",
    "FinalMutationProposal",
    "guard_final_mutation_proposal",
    "preview_final_mutation",
]
