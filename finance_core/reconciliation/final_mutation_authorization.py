"""Persisted authorization loading for guarded reconciliation final writes.

The final-write workflow accepts caller objects only as a requested operation.
This module reloads the authorization chain from the same SQLite connection and
fails closed when any persisted link is missing, malformed, inconsistent, or
does not bind the exact requested content.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from finance_core.money import canonical_decimal_str, canonical_money_str
from finance_core.reconciliation.final_mutation_money import (
    CreateMoneyValidationError,
    ValidatedReconciliationCreateMoney,
    validate_reconciliation_create_money,
)
from finance_core.reconciliation.final_mutation_proposal import (
    FinalMutationAction,
    FinalMutationProposal,
)


class PersistedAuthorizationError(ValueError):
    """A persisted authorization record cannot authorize the requested write."""

    code = "persisted_authorization_malformed"


class AuthorizationMissingError(PersistedAuthorizationError):
    code = "authorization_missing"


class AuthorizationStateDeniedError(PersistedAuthorizationError):
    code = "authorization_state_denied"


class AuthorizationMalformedError(PersistedAuthorizationError):
    code = "authorization_malformed"


class HumanConfirmationMissingError(PersistedAuthorizationError):
    code = "human_confirmation_missing"


class HumanConfirmationStateDeniedError(PersistedAuthorizationError):
    code = "human_confirmation_state_denied"


class HumanConfirmationSubjectMismatchError(PersistedAuthorizationError):
    code = "human_confirmation_subject_mismatch"


class HumanConfirmationOperationMismatchError(PersistedAuthorizationError):
    code = "human_confirmation_operation_mismatch"


class HumanConfirmationContentMismatchError(PersistedAuthorizationError):
    code = "human_confirmation_content_mismatch"


class GuardDecisionMismatchError(PersistedAuthorizationError):
    code = "guard_decision_mismatch"


class GuardedExecutionMismatchError(PersistedAuthorizationError):
    code = "guarded_execution_mismatch"


class AuthorizationAmbiguousError(PersistedAuthorizationError):
    code = "authorization_ambiguous"


@dataclass(frozen=True)
class PersistedFinalMutationAuthorization:
    authorization_id: str
    human_confirmation_id: str
    guard_decision_idempotency_key: str
    guarded_execution_id: str


def build_final_mutation_content_hash(proposal: FinalMutationProposal) -> str:
    """Hash only the material final-mutation content in canonical form."""
    normalized_currency = proposal.currency
    canonical_amount: str | None
    if proposal.action == FinalMutationAction.CREATE_FINAL_TRANSACTION:
        validated_money = validate_reconciliation_create_money(
            proposal.amount,
            proposal.currency,
        )
        canonical_amount = validated_money.canonical_amount
        normalized_currency = validated_money.currency
    elif proposal.amount is None:
        canonical_amount = None
    elif proposal.currency:
        canonical_amount = canonical_money_str(proposal.amount, proposal.currency)
    else:
        canonical_amount = canonical_decimal_str(proposal.amount)
    payload: dict[str, Any] = {
        "proposal_id": proposal.proposal_id,
        "action": proposal.action.value,
        "amount": canonical_amount,
        "currency": normalized_currency,
        "merchant": proposal.merchant,
        "transaction_date": proposal.transaction_date.isoformat()
        if proposal.transaction_date is not None
        else None,
        "target_transaction_id": proposal.target_transaction_id,
        "suggested_fields": dict(sorted(proposal.suggested_fields.items())),
        "source_statement_ref": proposal.source_statement_ref,
        "source_app_transaction_ref": proposal.source_app_transaction_ref,
        "evidence_refs": sorted(proposal.evidence_refs),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_legacy_v1_final_mutation_content_hash(
    proposal: FinalMutationProposal,
    *,
    persisted_money: ValidatedReconciliationCreateMoney | None = None,
    persisted_currency_representation: str | None = None,
) -> str:
    """Reproduce the pre-Money-Contract CREATE authorization hash.

    Compatibility is intentionally verifier-only. New records always use
    ``build_final_mutation_content_hash()`` and canonical CREATE currency.
    """
    amount = persisted_money.amount if persisted_money is not None else proposal.amount
    currency = (
        persisted_currency_representation if persisted_money is not None else proposal.currency
    )
    if amount is None:
        canonical_amount: str | None = None
    elif currency:
        canonical_amount = canonical_money_str(amount, currency)
    else:
        canonical_amount = canonical_decimal_str(amount)
    payload: dict[str, Any] = {
        "proposal_id": proposal.proposal_id,
        "action": proposal.action.value,
        "amount": canonical_amount,
        "currency": currency,
        "merchant": proposal.merchant,
        "transaction_date": (
            proposal.transaction_date.isoformat() if proposal.transaction_date is not None else None
        ),
        "target_transaction_id": proposal.target_transaction_id,
        "suggested_fields": dict(sorted(proposal.suggested_fields.items())),
        "source_statement_ref": proposal.source_statement_ref,
        "source_app_transaction_ref": proposal.source_app_transaction_ref,
        "evidence_refs": sorted(proposal.evidence_refs),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_persisted_final_mutation_authorization(
    conn: sqlite3.Connection,
    *,
    authorization_id: str,
    plan_id: str,
    operation_id: str,
    proposal: FinalMutationProposal,
    human_confirmation_id: str,
) -> PersistedFinalMutationAuthorization:
    """Load and validate the complete persisted final-write authority chain."""
    if not authorization_id:
        raise AuthorizationMissingError()
    try:
        row = conn.execute(
            """
            SELECT authorization_id, subject_type, subject_id, operation_type,
                   proposal_id, plan_id, guard_decision_idempotency_key,
                   guarded_execution_id, human_confirmation_id, content_hash,
                   authorization_state, authorization_version
            FROM reconciliation_final_mutation_authorizations
            WHERE authorization_id = ?
            """,
            (authorization_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise AuthorizationMissingError() from exc
    if row is None:
        raise AuthorizationMissingError()
    if row[10] != "authorized":
        raise AuthorizationStateDeniedError()
    if not row[11] or len(row[9]) != 64:
        raise AuthorizationMalformedError()
    if row[1] != "reconciliation_apply_operation" or row[2] != operation_id:
        raise HumanConfirmationSubjectMismatchError()
    if row[3] != proposal.action.value or row[4] != proposal.proposal_id or row[5] != plan_id:
        raise HumanConfirmationOperationMismatchError()
    if row[8] != human_confirmation_id:
        raise HumanConfirmationOperationMismatchError()
    current_content_hash = build_final_mutation_content_hash(proposal)
    requires_legacy_hash_verification = row[9] != current_content_hash
    if (
        requires_legacy_hash_verification
        and proposal.action != FinalMutationAction.CREATE_FINAL_TRANSACTION
    ):
        raise HumanConfirmationContentMismatchError()

    confirmation = conn.execute(
        """
        SELECT subject_type, subject_id, operation_type, proposal_id, plan_id,
               content_hash, confirmation_state
        FROM reconciliation_final_mutation_confirmations
        WHERE confirmation_id = ?
        """,
        (row[8],),
    ).fetchall()
    if not confirmation:
        raise HumanConfirmationMissingError()
    if len(confirmation) != 1:
        raise AuthorizationAmbiguousError()
    confirmation_row = confirmation[0]
    if confirmation_row[6] != "confirmed":
        raise HumanConfirmationStateDeniedError()
    if confirmation_row[0] != row[1] or confirmation_row[1] != row[2]:
        raise HumanConfirmationSubjectMismatchError()
    if (
        confirmation_row[2] != row[3]
        or confirmation_row[3] != row[4]
        or confirmation_row[4] != row[5]
    ):
        raise HumanConfirmationOperationMismatchError()
    if confirmation_row[5] != row[9]:
        raise HumanConfirmationContentMismatchError()

    decision = conn.execute(
        """
        SELECT proposal_id, action, approved, preview_json, evidence_refs_json
        FROM reconciliation_final_mutation_guard_decisions
        WHERE idempotency_key = ?
        """,
        (row[6],),
    ).fetchall()
    if len(decision) != 1:
        raise AuthorizationAmbiguousError()
    decision_row = decision[0]
    if decision_row[0] != proposal.proposal_id or decision_row[1] != proposal.action.value:
        raise GuardDecisionMismatchError()
    if decision_row[2] != 1 or not decision_row[3]:
        raise GuardDecisionMismatchError()
    try:
        preview = json.loads(decision_row[3])
        evidence = json.loads(decision_row[4])
    except (TypeError, json.JSONDecodeError) as exc:
        raise AuthorizationMalformedError() from exc
    if not isinstance(preview, dict) or not isinstance(evidence, list):
        raise AuthorizationMalformedError()
    if (
        preview.get("proposal_id") != proposal.proposal_id
        or preview.get("action") != proposal.action.value
    ):
        raise GuardDecisionMismatchError()
    if proposal.action == FinalMutationAction.CREATE_FINAL_TRANSACTION:
        try:
            persisted_preview_money = _validate_persisted_create_preview(preview, proposal)
        except GuardDecisionMismatchError as exc:
            if requires_legacy_hash_verification:
                raise HumanConfirmationContentMismatchError() from exc
            raise
        if requires_legacy_hash_verification:
            legacy_content_hash = _build_legacy_v1_final_mutation_content_hash(
                proposal,
                persisted_money=persisted_preview_money,
                persisted_currency_representation=preview["currency"],
            )
            if row[9] != legacy_content_hash:
                raise HumanConfirmationContentMismatchError()
    if sorted(evidence) != sorted(proposal.evidence_refs):
        raise GuardDecisionMismatchError()

    operation = conn.execute(
        """
        SELECT e.plan_id, e.execution_status, e.is_dry_run,
               o.execution_status, o.guard_decision_approved,
               o.guard_decision_idempotency_key
        FROM reconciliation_guarded_apply_executions AS e
        JOIN reconciliation_guarded_apply_operation_results AS o
          ON o.execution_id = e.execution_id
        WHERE e.execution_id = ? AND o.operation_id = ?
        """,
        (row[7], operation_id),
    ).fetchall()
    if len(operation) != 1:
        raise AuthorizationAmbiguousError()
    op = operation[0]
    if op[0] != plan_id or op[1] != "executed" or op[2] != 1:
        raise GuardedExecutionMismatchError()
    if op[3] != "executed" or op[4] != 1 or op[5] != row[6]:
        raise GuardedExecutionMismatchError()
    return PersistedFinalMutationAuthorization(
        authorization_id=row[0],
        human_confirmation_id=row[8],
        guard_decision_idempotency_key=row[6],
        guarded_execution_id=row[7],
    )


def _validate_persisted_create_preview(
    preview: dict[str, Any],
    proposal: FinalMutationProposal,
) -> ValidatedReconciliationCreateMoney:
    """Strictly validate current or legacy-v1 persisted CREATE preview material."""
    required_keys = {
        "proposal_id",
        "action",
        "amount",
        "currency",
        "merchant",
        "transaction_date",
        "target_transaction_id",
        "suggested_fields",
        "source_statement_ref",
        "source_app_transaction_ref",
        "evidence_refs",
        "is_dry_run",
    }
    allowed_keys = required_keys | {"preview_note"}
    if not required_keys.issubset(preview) or not set(preview).issubset(allowed_keys):
        raise GuardDecisionMismatchError()
    if not isinstance(preview["amount"], str) or not isinstance(preview["currency"], str):
        raise GuardDecisionMismatchError()
    if not isinstance(preview["suggested_fields"], dict):
        raise GuardDecisionMismatchError()
    if not isinstance(preview["evidence_refs"], list) or not all(
        isinstance(item, str) for item in preview["evidence_refs"]
    ):
        raise GuardDecisionMismatchError()
    if type(preview["is_dry_run"]) is not bool or preview["is_dry_run"] is not True:
        raise GuardDecisionMismatchError()
    if "preview_note" in preview and not isinstance(preview["preview_note"], str):
        raise GuardDecisionMismatchError()

    current_money = validate_reconciliation_create_money(
        proposal.amount,
        proposal.currency,
    )
    try:
        persisted_money = validate_reconciliation_create_money(
            preview["amount"],
            preview["currency"],
        )
    except CreateMoneyValidationError as exc:
        raise GuardDecisionMismatchError() from exc
    if persisted_money != current_money:
        raise GuardDecisionMismatchError()

    expected = {
        "proposal_id": proposal.proposal_id,
        "action": proposal.action.value,
        "merchant": proposal.merchant,
        "transaction_date": (
            proposal.transaction_date.isoformat() if proposal.transaction_date is not None else None
        ),
        "target_transaction_id": proposal.target_transaction_id,
        "suggested_fields": dict(sorted(proposal.suggested_fields.items())),
        "source_statement_ref": proposal.source_statement_ref,
        "source_app_transaction_ref": proposal.source_app_transaction_ref,
        "evidence_refs": sorted(proposal.evidence_refs),
        "is_dry_run": True,
    }
    if any(preview[key] != value for key, value in expected.items()):
        raise GuardDecisionMismatchError()
    return persisted_money


__all__ = [
    "PersistedAuthorizationError",
    "AuthorizationMissingError",
    "AuthorizationStateDeniedError",
    "AuthorizationMalformedError",
    "HumanConfirmationMissingError",
    "HumanConfirmationStateDeniedError",
    "HumanConfirmationSubjectMismatchError",
    "HumanConfirmationOperationMismatchError",
    "HumanConfirmationContentMismatchError",
    "GuardDecisionMismatchError",
    "GuardedExecutionMismatchError",
    "AuthorizationAmbiguousError",
    "PersistedFinalMutationAuthorization",
    "build_final_mutation_content_hash",
    "load_persisted_final_mutation_authorization",
]
