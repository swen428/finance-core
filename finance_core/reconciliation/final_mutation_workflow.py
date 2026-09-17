"""Guarded Final Mutation Workflow v1 -- bounded final-write boundary that
accepts a fully approved reconciliation final mutation proposal, validates
all preconditions, enforces deterministic idempotency, and executes the
approved write to a caller-supplied SQLite connection.

This is the first runtime implementation of the designed final-write
boundary from docs/design/reconciliation_guarded_final_mutation_workflow_v1.md.

Key invariants:
- Accepts an explicit ``sqlite3.Connection``; never opens ``database/finance.db``
  or resolves a default database path.
- Every final write requires all preconditions: approved guard decision,
  matching guarded apply execution result, explicit human final-write
  confirmation, and a deterministic idempotency key.
- Only CREATE_FINAL_TRANSACTION and ADJUST_FINAL_TRANSACTION are supported in v1.
- All unsupported or unsafe actions are blocked without final writes.
- Deterministic idempotency: same key + same content returns already-finalized
  or equivalent safe result; same key + different content returns conflict.
- Monetary values use Decimal-safe string representations.
- Audit refs are preserved in the result and in the caller-supplied connection.
- This workflow is separate from ``GuardedApplyRuntime`` -- it does not turn
  the guarded dry-run runtime into a live writer.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, cast

from finance_core.financial_audit import (
    AuditEventCommand,
    FinancialAuditRepository,
    append_financial_audit_event,
    derive_audit_event_public_id,
)
from finance_core.money import (
    MoneyValidationError,
    canonical_decimal_str,
    money_decimal,
    validate_amount_for_currency,
)
from finance_core.reconciliation.apply_plan import (
    ReconciliationApplyPlan,
)
from finance_core.reconciliation.final_mutation_authorization import (
    PersistedAuthorizationError,
    load_persisted_final_mutation_authorization,
)
from finance_core.reconciliation.final_mutation_money import (
    CreateMoneyValidationError,
    CreateMoneyValidationIssue,
    ValidatedReconciliationCreateMoney,
    validate_reconciliation_create_money,
)
from finance_core.reconciliation.final_mutation_proposal import (
    FinalMutationAction,
    FinalMutationGuardDecision,
    FinalMutationProposal,
)
from finance_core.reconciliation.models import (
    ApplyExecutionStatus,
    GuardedApplyExecutionResult,
)
from finance_core.staging_guard import require_staging_database


class FinalMutationPersistenceError(RuntimeError):
    """Required schema is missing — migration 019 must be applied before the
    final-mutation workflow runs.  Runtime DDL is intentionally not performed."""


class FinalMutationTransactionError(RuntimeError):
    """The caller supplied a connection with an already active transaction."""


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class FinalMutationWorkflowStatus(str, Enum):
    """Stable status codes for a final mutation workflow attempt."""

    FINALIZED = "finalized"
    """The mutation was approved, confirmed, and executed exactly once."""

    BLOCKED = "blocked"
    """The mutation was blocked by one or more precondition failures."""

    CONFLICT = "conflict"
    """The same idempotency key was used with different material content."""

    ALREADY_FINALIZED = "already_finalized"
    """The same idempotency key with the same content was already finalized."""


# ---------------------------------------------------------------------------
# Block reason codes
# ---------------------------------------------------------------------------


class FinalMutationWorkflowBlockReason(str, Enum):
    """Stable reason codes emitted when the workflow blocks a mutation attempt.

    These extend the guard-level ``FinalMutationBlockedReason`` codes with
    workflow-specific preconditions that must be satisfied before any final
    write can proceed.
    """

    MISSING_HUMAN_CONFIRMATION = "missing_human_confirmation"
    GUARD_NOT_APPROVED = "guard_not_approved"
    GUARD_OPERATION_MISMATCH = "guard_operation_mismatch"
    EXECUTION_NOT_SUCCESSFUL = "execution_not_successful"
    UNSUPPORTED_ACTION = "unsupported_action"
    MISSING_EVIDENCE_REFS = "missing_evidence_refs"
    OPERATION_NOT_IN_PLAN = "operation_not_in_plan"
    EMPTY_IDEMPOTENCY_KEY = "empty_idempotency_key"
    TARGET_TRANSACTION_NOT_FOUND = "target_transaction_not_found"
    TARGET_TRANSACTION_AMBIGUOUS = "target_transaction_ambiguous"
    UNSAFE_DATABASE_TARGET = "unsafe_database_target"
    MISSING_PERSISTED_AUTHORIZATION = "missing_persisted_authorization"
    PERSISTED_AUTHORIZATION_DENIED = "persisted_authorization_denied"
    PERSISTED_AUTHORIZATION_MISMATCH = "persisted_authorization_mismatch"
    PERSISTED_AUTHORIZATION_MALFORMED = "persisted_authorization_malformed"
    #
    # CREATE required-field validation (workflow-level, independent of guard)
    CREATE_MISSING_AMOUNT = "create_missing_amount"
    CREATE_MISSING_CURRENCY = "create_missing_currency"
    CREATE_MISSING_DATE = "create_missing_date"
    CREATE_MISSING_MERCHANT = "create_missing_merchant"
    CREATE_MISSING_SOURCE_REF = "create_missing_source_ref"
    CREATE_INVALID_AMOUNT = "create_invalid_amount"
    CREATE_INVALID_CURRENCY = "create_invalid_currency"
    #
    MISSING_GUARD_EXECUTION_RESULT = "missing_guard_execution_result"
    EXECUTION_RESULT_CONFLICT = "execution_result_conflict"
    EXECUTION_RESULT_NO_MATCHING_OP = "execution_result_no_matching_op"
    CONFLICTING_FINAL_MUTATION = "conflicting_final_mutation"
    DUPLICATE_FINAL_MUTATION = "duplicate_final_mutation"
    ADJUST_FIELD_NOT_ALLOWED = "adjust_field_not_allowed"
    ADJUST_NO_ALLOWED_FIELDS = "adjust_no_allowed_fields"
    INVALID_ADJUSTMENT_AMOUNT = "invalid_adjustment_amount"
    MISSING_SCHEMA = "missing_schema"


# ---------------------------------------------------------------------------
# Input DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalMutationWorkflowInput:
    """Typed input for one final mutation workflow attempt.

    Every field except ``actor_id`` is required. This ensures callers cannot
    accidentally skip precondition evidence.
    """

    plan: ReconciliationApplyPlan
    operation_id: str
    proposal: FinalMutationProposal
    guard_decision: FinalMutationGuardDecision
    guard_execution_result: GuardedApplyExecutionResult
    human_confirmation_id: str
    idempotency_key: str
    actor_type: str = "system"
    actor_id: str | None = None
    authorization_id: str = ""


# ---------------------------------------------------------------------------
# Result DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalMutationWorkflowResult:
    """Output DTO from ``execute_guarded_final_mutation_workflow()``."""

    final_mutation_id: str
    operation_id: str
    proposal_id: str
    action: str
    status: FinalMutationWorkflowStatus
    transaction_public_id: str | None = None
    idempotency_key: str = ""
    idempotency_fingerprint: str = ""
    audit_refs: dict[str, object] = field(default_factory=dict)
    blocked_reasons: tuple[str, ...] = field(default_factory=tuple)
    created_at: str = ""


# ---------------------------------------------------------------------------
# Supported v1 actions
# ---------------------------------------------------------------------------

_SUPPORTED_ACTIONS: frozenset[str] = frozenset(
    {
        FinalMutationAction.CREATE_FINAL_TRANSACTION.value,
        FinalMutationAction.ADJUST_FINAL_TRANSACTION.value,
    }
)

_CURRENT_CREATE_FINGERPRINT_VERSION = "final-mutation-v2"
_LEGACY_FINGERPRINT_VERSION = "final-mutation-v1"


@dataclass(frozen=True)
class _PersistedFinalMutationAuditRecord:
    idempotency_fingerprint: object
    mutation_payload_json: object
    operation_id: object
    proposal_id: object
    guard_version: object
    human_confirmation_id: object
    action: object
    evidence_refs_json: object
    idempotency_key: object
    status: object
    transaction_public_id: object
    audit_refs_json: object
    final_mutation_id: object
    created_at: object
    blocked_reasons_json: object


_ADJUST_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "amount",
        "currency",
        "transaction_date",
        "merchant",
        "category",
        "notes",
    }
)

_FORBIDDEN_FIELD_MARKERS: frozenset[str] = frozenset(
    {
        "status",
        "settlement",
        "obligation",
        "parser",
        "calculation",
        "sql",
        "payload",
        "lifecycle",
    }
)


# Legacy parallel table is intentionally NOT created or written to.
# Canonical financial facts belong in ``transactions`` (migration 001).
# The audit table ``reconciliation_final_mutation_audit`` is created by
# migration 019 -- no runtime DDL is performed by this workflow.


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def execute_guarded_final_mutation_workflow(
    conn: sqlite3.Connection,
    workflow_input: FinalMutationWorkflowInput,
    *,
    clock: Callable[[], str] | None = None,
) -> FinalMutationWorkflowResult:
    """Execute a guarded final mutation workflow attempt.

    This is the single public entry point for the final-write boundary.
    It must only write to an authorised staging database.
    It accepts an explicit ``sqlite3.Connection`` and a typed
    ``FinalMutationWorkflowInput``, validates every precondition, enforces
    deterministic idempotency, and either executes the approved write or
    returns a blocked result with stable reason codes.

    The workflow never opens ``database/finance.db``, never infers a
    default database path, and never performs network calls.
    """
    require_staging_database(conn)
    if conn.in_transaction:
        raise FinalMutationTransactionError(
            "Final mutation requires a connection without pending work; "
            "the caller-owned transaction was left active and unchanged."
        )

    created_at = clock() if clock is not None else datetime.now(timezone.utc).isoformat()

    if not workflow_input.idempotency_key:
        return _build_and_maybe_persist_blocked_result(
            conn,
            workflow_input,
            created_at,
            (FinalMutationWorkflowBlockReason.EMPTY_IDEMPOTENCY_KEY.value,),
        )

    if not workflow_input.human_confirmation_id:
        return _build_and_maybe_persist_blocked_result(
            conn,
            workflow_input,
            created_at,
            (FinalMutationWorkflowBlockReason.MISSING_HUMAN_CONFIRMATION.value,),
        )

    if workflow_input.proposal.action.value not in _SUPPORTED_ACTIONS:
        return _build_and_maybe_persist_blocked_result(
            conn,
            workflow_input,
            created_at,
            (FinalMutationWorkflowBlockReason.UNSUPPORTED_ACTION.value,),
        )

    block_reasons = _validate_preconditions(workflow_input)
    if block_reasons:
        return _build_and_maybe_persist_blocked_result(
            conn,
            workflow_input,
            created_at,
            block_reasons,
        )

    validated_create_money = (
        validate_reconciliation_create_money(
            workflow_input.proposal.amount,
            workflow_input.proposal.currency,
        )
        if workflow_input.proposal.action == FinalMutationAction.CREATE_FINAL_TRANSACTION
        else None
    )

    try:
        conn.execute("BEGIN IMMEDIATE")
        _persisted_authorization = load_persisted_final_mutation_authorization(
            conn,
            authorization_id=workflow_input.authorization_id,
            plan_id=workflow_input.plan.plan_id,
            operation_id=workflow_input.operation_id,
            proposal=workflow_input.proposal,
            human_confirmation_id=workflow_input.human_confirmation_id,
        )
    except PersistedAuthorizationError as exc:
        if conn.in_transaction:
            conn.rollback()
        missing_authorization = (
            FinalMutationWorkflowBlockReason.MISSING_PERSISTED_AUTHORIZATION.value
        )
        denied_authorization = FinalMutationWorkflowBlockReason.PERSISTED_AUTHORIZATION_DENIED.value
        mismatched_authorization = (
            FinalMutationWorkflowBlockReason.PERSISTED_AUTHORIZATION_MISMATCH.value
        )
        reason = {
            "authorization_missing": missing_authorization,
            "authorization_state_denied": denied_authorization,
            "human_confirmation_state_denied": denied_authorization,
            "human_confirmation_missing": missing_authorization,
            "human_confirmation_subject_mismatch": mismatched_authorization,
            "human_confirmation_operation_mismatch": mismatched_authorization,
            "human_confirmation_content_mismatch": mismatched_authorization,
            "guard_decision_mismatch": mismatched_authorization,
            "guarded_execution_mismatch": mismatched_authorization,
        }.get(exc.code, FinalMutationWorkflowBlockReason.PERSISTED_AUTHORIZATION_MALFORMED.value)
        return _build_and_maybe_persist_blocked_result(conn, workflow_input, created_at, (reason,))
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()
        return _build_and_maybe_persist_blocked_result(
            conn,
            workflow_input,
            created_at,
            (FinalMutationWorkflowBlockReason.PERSISTED_AUTHORIZATION_MALFORMED.value,),
        )

    try:
        fingerprint = _build_final_mutation_fingerprint(
            workflow_input,
            validated_create_money=validated_create_money,
        )

        if not _table_exists(conn, "reconciliation_final_mutation_audit") or not _table_exists(
            conn, "financial_audit_events"
        ):
            conn.rollback()
            return _build_blocked_result(
                workflow_input,
                created_at,
                (FinalMutationWorkflowBlockReason.MISSING_SCHEMA.value,),
            )

        idem_check = _check_durable_idempotency(
            conn,
            workflow_input,
            fingerprint,
            created_at,
            validated_create_money=validated_create_money,
        )
        if idem_check is not None:
            conn.rollback()
            return idem_check

        return _execute_mutation(
            conn,
            workflow_input,
            fingerprint,
            created_at,
            validated_create_money=validated_create_money,
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Precondition validation
# ---------------------------------------------------------------------------


def _validate_preconditions(inp: FinalMutationWorkflowInput) -> tuple[str, ...]:
    """Validate all final-write preconditions before any mutation.

    Returns a tuple of stable reason codes. An empty tuple means all
    preconditions are satisfied.
    """
    reasons: list[str] = []

    matching_ops = [op for op in inp.plan.operations if op.operation_id == inp.operation_id]
    if len(matching_ops) != 1:
        reasons.append(FinalMutationWorkflowBlockReason.OPERATION_NOT_IN_PLAN.value)

    action_value = inp.proposal.action.value
    if action_value not in _SUPPORTED_ACTIONS:
        reasons.append(FinalMutationWorkflowBlockReason.UNSUPPORTED_ACTION.value)

    if not inp.guard_decision.approved:
        reasons.append(FinalMutationWorkflowBlockReason.GUARD_NOT_APPROVED.value)

    if inp.guard_decision.proposal_id != inp.proposal.proposal_id:
        reasons.append(FinalMutationWorkflowBlockReason.GUARD_OPERATION_MISMATCH.value)

    if inp.guard_decision.action != inp.proposal.action:
        reasons.append(FinalMutationWorkflowBlockReason.GUARD_OPERATION_MISMATCH.value)

    if matching_ops:
        op = matching_ops[0]
        if (
            op.source_apply_result_ref is not None
            and inp.guard_decision.proposal_id != op.source_apply_result_ref
        ):
            reasons.append(FinalMutationWorkflowBlockReason.GUARD_OPERATION_MISMATCH.value)

    exec_result = inp.guard_execution_result
    if exec_result.execution_status == ApplyExecutionStatus.CONFLICT:
        reasons.append(FinalMutationWorkflowBlockReason.EXECUTION_RESULT_CONFLICT.value)
    elif exec_result.execution_status in (
        ApplyExecutionStatus.BLOCKED,
        ApplyExecutionStatus.UNSUPPORTED,
    ):
        reasons.append(FinalMutationWorkflowBlockReason.EXECUTION_NOT_SUCCESSFUL.value)

    exec_op_matches = [r for r in exec_result.results if r.operation_id == inp.operation_id]
    if len(exec_op_matches) == 0:
        reasons.append(FinalMutationWorkflowBlockReason.EXECUTION_RESULT_NO_MATCHING_OP.value)
    elif exec_op_matches[0].execution_status != ApplyExecutionStatus.EXECUTED:
        reasons.append(FinalMutationWorkflowBlockReason.EXECUTION_NOT_SUCCESSFUL.value)

    if not inp.human_confirmation_id:
        reasons.append(FinalMutationWorkflowBlockReason.MISSING_HUMAN_CONFIRMATION.value)

    if not inp.proposal.evidence_refs:
        reasons.append(FinalMutationWorkflowBlockReason.MISSING_EVIDENCE_REFS.value)

    # ── CREATE required-field validation ──────────────────────────
    # Independent workflow check -- the guard may have approved, but the
    # workflow must still enforce that every required CREATE field is present.
    if action_value == FinalMutationAction.CREATE_FINAL_TRANSACTION.value:
        proposal = inp.proposal
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
            reasons.append(FinalMutationWorkflowBlockReason.CREATE_MISSING_AMOUNT.value)
        elif CreateMoneyValidationIssue.AMOUNT in money_issues:
            reasons.append(FinalMutationWorkflowBlockReason.CREATE_INVALID_AMOUNT.value)
        if currency_missing:
            reasons.append(FinalMutationWorkflowBlockReason.CREATE_MISSING_CURRENCY.value)
        elif CreateMoneyValidationIssue.CURRENCY in money_issues:
            reasons.append(FinalMutationWorkflowBlockReason.CREATE_INVALID_CURRENCY.value)
        if proposal.transaction_date is None:
            reasons.append(FinalMutationWorkflowBlockReason.CREATE_MISSING_DATE.value)
        if not proposal.merchant:
            reasons.append(FinalMutationWorkflowBlockReason.CREATE_MISSING_MERCHANT.value)
        if not proposal.source_statement_ref:
            reasons.append(FinalMutationWorkflowBlockReason.CREATE_MISSING_SOURCE_REF.value)

    # ── ADJUST field validation ──────────────────────────────────
    if action_value == FinalMutationAction.ADJUST_FINAL_TRANSACTION.value:
        field_errors = _validate_adjust_fields(inp.proposal.suggested_fields)
        if field_errors:
            reasons.append(FinalMutationWorkflowBlockReason.ADJUST_FIELD_NOT_ALLOWED.value)
        allowed_count = sum(1 for f in inp.proposal.suggested_fields if f in _ADJUST_ALLOWED_FIELDS)
        if allowed_count == 0:
            reasons.append(FinalMutationWorkflowBlockReason.ADJUST_NO_ALLOWED_FIELDS.value)

    return tuple(reasons)


# ---------------------------------------------------------------------------
# Durable idempotency check
# ---------------------------------------------------------------------------


def _check_durable_idempotency(
    conn: sqlite3.Connection,
    inp: FinalMutationWorkflowInput,
    fingerprint: str,
    created_at: str,
    *,
    validated_create_money: ValidatedReconciliationCreateMoney | None,
) -> FinalMutationWorkflowResult | None:
    """Load current-v2 or strictly verified legacy-v1 durable identity."""
    row = conn.execute(
        """
        SELECT idempotency_fingerprint, mutation_payload_json,
               operation_id, proposal_id, guard_version,
               human_confirmation_id, action, evidence_refs_json,
               idempotency_key, status, transaction_public_id,
               audit_refs_json, final_mutation_id, created_at,
               blocked_reasons_json
        FROM reconciliation_final_mutation_audit
        WHERE idempotency_key = ?
        """,
        (inp.idempotency_key,),
    ).fetchone()

    if row is None:
        return None

    record = _PersistedFinalMutationAuditRecord(*tuple(row))
    equivalent = False
    try:
        if record.idempotency_fingerprint == fingerprint:
            if inp.proposal.action != FinalMutationAction.CREATE_FINAL_TRANSACTION:
                return _build_existing_adjust_replay_result(record, inp, fingerprint)
            equivalent = _current_record_material_matches(
                record,
                inp,
                validated_create_money=validated_create_money,
            )
        elif inp.proposal.action == FinalMutationAction.CREATE_FINAL_TRANSACTION:
            equivalent = _legacy_v1_create_record_matches(
                record,
                inp,
                validated_create_money=validated_create_money,
            )
        replay = _build_persisted_replay_result(record, inp) if equivalent else None
    except (json.JSONDecodeError, TypeError, ValueError):
        replay = None

    if replay is not None:
        return replay
    return _build_idempotency_conflict_result(inp, fingerprint, created_at)


def _build_existing_adjust_replay_result(
    record: _PersistedFinalMutationAuditRecord,
    inp: FinalMutationWorkflowInput,
    fingerprint: str,
) -> FinalMutationWorkflowResult:
    """Preserve the exact-fingerprint ADJUST replay behavior from v1."""
    final_mutation_id = _derive_final_mutation_id(inp.idempotency_key)
    audit_refs = _deserialize_json_dict(cast(str | None, record.audit_refs_json))
    created_at = cast(str, record.created_at)
    if record.status == FinalMutationWorkflowStatus.BLOCKED.value:
        return FinalMutationWorkflowResult(
            final_mutation_id=final_mutation_id,
            operation_id=inp.operation_id,
            proposal_id=inp.proposal.proposal_id,
            action=inp.proposal.action.value,
            status=FinalMutationWorkflowStatus.BLOCKED,
            transaction_public_id=None,
            idempotency_key=inp.idempotency_key,
            idempotency_fingerprint=fingerprint,
            audit_refs=audit_refs,
            blocked_reasons=tuple(
                _deserialize_json_list(cast(str | None, record.blocked_reasons_json))
            ),
            created_at=created_at,
        )
    return FinalMutationWorkflowResult(
        final_mutation_id=final_mutation_id,
        operation_id=inp.operation_id,
        proposal_id=inp.proposal.proposal_id,
        action=inp.proposal.action.value,
        status=FinalMutationWorkflowStatus.ALREADY_FINALIZED,
        transaction_public_id=cast(str | None, record.transaction_public_id),
        idempotency_key=inp.idempotency_key,
        idempotency_fingerprint=fingerprint,
        audit_refs=audit_refs,
        blocked_reasons=(),
        created_at=created_at,
    )


def _current_record_material_matches(
    record: _PersistedFinalMutationAuditRecord,
    inp: FinalMutationWorkflowInput,
    *,
    validated_create_money: ValidatedReconciliationCreateMoney | None,
) -> bool:
    if not _record_identity_matches(record, inp):
        return False
    stored_payload = _strict_json_dict(record.mutation_payload_json)
    expected_payload = _build_mutation_payload(
        inp,
        validated_create_money=validated_create_money,
    )
    return stored_payload == expected_payload


def _legacy_v1_create_record_matches(
    record: _PersistedFinalMutationAuditRecord,
    inp: FinalMutationWorkflowInput,
    *,
    validated_create_money: ValidatedReconciliationCreateMoney | None,
) -> bool:
    if validated_create_money is None or not _record_identity_matches(record, inp):
        return False
    stored_payload = _strict_json_dict(record.mutation_payload_json)
    stored_evidence = _strict_json_string_list(record.evidence_refs_json)
    if not isinstance(record.idempotency_fingerprint, str):
        return False
    if record.idempotency_fingerprint != _build_legacy_v1_persisted_fingerprint(
        record,
        stored_payload,
        stored_evidence,
    ):
        return False

    if set(stored_payload) != set(_build_legacy_v1_mutation_payload(inp)):
        return False
    stored_amount = stored_payload.get("amount")
    stored_currency = stored_payload.get("currency")
    if not isinstance(stored_amount, str) or not isinstance(stored_currency, str):
        return False
    try:
        stored_money = validate_reconciliation_create_money(stored_amount, stored_currency)
    except CreateMoneyValidationError:
        return False
    if stored_money != validated_create_money:
        return False

    stored_non_money = {
        key: value for key, value in stored_payload.items() if key not in {"amount", "currency"}
    }
    expected_non_money = {
        key: value
        for key, value in _build_legacy_v1_mutation_payload(inp).items()
        if key not in {"amount", "currency"}
    }
    return stored_non_money == expected_non_money


def _record_identity_matches(
    record: _PersistedFinalMutationAuditRecord,
    inp: FinalMutationWorkflowInput,
) -> bool:
    stored_evidence = _strict_json_string_list(record.evidence_refs_json)
    return (
        record.operation_id == inp.operation_id
        and record.proposal_id == inp.proposal.proposal_id
        and record.guard_version == inp.guard_decision.guard_version
        and record.human_confirmation_id == inp.human_confirmation_id
        and record.action == inp.proposal.action.value
        and record.idempotency_key == inp.idempotency_key
        and sorted(stored_evidence) == sorted(inp.proposal.evidence_refs)
    )


def _build_legacy_v1_persisted_fingerprint(
    record: _PersistedFinalMutationAuditRecord,
    mutation_payload: dict[str, object],
    evidence_refs: list[str],
) -> str:
    canonical = json.dumps(
        {
            "version": _LEGACY_FINGERPRINT_VERSION,
            "operation_id": record.operation_id,
            "proposal_id": record.proposal_id,
            "guard_version": record.guard_version,
            "human_confirmation_id": record.human_confirmation_id,
            "action": record.action,
            "mutation_payload": mutation_payload,
            "evidence_refs": sorted(evidence_refs),
            "idempotency_key": record.idempotency_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_persisted_replay_result(
    record: _PersistedFinalMutationAuditRecord,
    inp: FinalMutationWorkflowInput,
) -> FinalMutationWorkflowResult | None:
    if (
        not isinstance(record.final_mutation_id, str)
        or record.final_mutation_id != _derive_final_mutation_id(inp.idempotency_key)
        or not isinstance(record.idempotency_fingerprint, str)
        or not isinstance(record.created_at, str)
    ):
        return None
    audit_refs = _strict_json_dict(record.audit_refs_json)
    blocked_reasons = tuple(_strict_json_string_list(record.blocked_reasons_json))
    if record.status == FinalMutationWorkflowStatus.BLOCKED.value:
        if record.transaction_public_id is not None or not blocked_reasons:
            return None
        status = FinalMutationWorkflowStatus.BLOCKED
        transaction_public_id = None
    elif record.status == FinalMutationWorkflowStatus.FINALIZED.value:
        if not isinstance(record.transaction_public_id, str) or not record.transaction_public_id:
            return None
        if blocked_reasons:
            return None
        status = FinalMutationWorkflowStatus.ALREADY_FINALIZED
        transaction_public_id = record.transaction_public_id
    else:
        return None
    return FinalMutationWorkflowResult(
        final_mutation_id=record.final_mutation_id,
        operation_id=inp.operation_id,
        proposal_id=inp.proposal.proposal_id,
        action=inp.proposal.action.value,
        status=status,
        transaction_public_id=transaction_public_id,
        idempotency_key=inp.idempotency_key,
        idempotency_fingerprint=record.idempotency_fingerprint,
        audit_refs=audit_refs,
        blocked_reasons=blocked_reasons,
        created_at=record.created_at,
    )


def _build_idempotency_conflict_result(
    inp: FinalMutationWorkflowInput,
    fingerprint: str,
    created_at: str,
) -> FinalMutationWorkflowResult:
    return FinalMutationWorkflowResult(
        final_mutation_id=_derive_final_mutation_id(inp.idempotency_key),
        operation_id=inp.operation_id,
        proposal_id=inp.proposal.proposal_id,
        action=inp.proposal.action.value,
        status=FinalMutationWorkflowStatus.CONFLICT,
        transaction_public_id=None,
        idempotency_key=inp.idempotency_key,
        idempotency_fingerprint=fingerprint,
        audit_refs=_build_audit_refs(inp),
        blocked_reasons=(FinalMutationWorkflowBlockReason.CONFLICTING_FINAL_MUTATION.value,),
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Mutation execution
# ---------------------------------------------------------------------------


def _execute_mutation(
    conn: sqlite3.Connection,
    inp: FinalMutationWorkflowInput,
    fingerprint: str,
    created_at: str,
    *,
    validated_create_money: ValidatedReconciliationCreateMoney | None,
) -> FinalMutationWorkflowResult:
    """Execute the approved final mutation exactly once."""
    action_value = inp.proposal.action.value
    final_mutation_id = _derive_final_mutation_id(inp.idempotency_key)
    transaction_public_id: str | None = None
    previous_set = False

    if action_value == FinalMutationAction.CREATE_FINAL_TRANSACTION.value:
        if validated_create_money is None:
            raise FinalMutationPersistenceError("Validated CREATE money is required")
        transaction_public_id = _execute_create(
            conn,
            inp,
            final_mutation_id,
            created_at,
            validated_create_money,
        )
    elif action_value == FinalMutationAction.ADJUST_FINAL_TRANSACTION.value:
        try:
            transaction_public_id, previous_set, previous_values = _execute_adjust(
                conn, inp, final_mutation_id, created_at
            )
        except MoneyValidationError:
            conn.rollback()
            return _build_and_maybe_persist_blocked_result(
                conn,
                inp,
                created_at,
                (FinalMutationWorkflowBlockReason.INVALID_ADJUSTMENT_AMOUNT.value,),
            )
        except ValueError as exc:
            msg = str(exc)
            if "not found" in msg:
                conn.rollback()
                return _build_and_maybe_persist_blocked_result(
                    conn,
                    inp,
                    created_at,
                    (FinalMutationWorkflowBlockReason.TARGET_TRANSACTION_NOT_FOUND.value,),
                )
            if "ambiguous" in msg:
                conn.rollback()
                return _build_and_maybe_persist_blocked_result(
                    conn,
                    inp,
                    created_at,
                    (FinalMutationWorkflowBlockReason.TARGET_TRANSACTION_AMBIGUOUS.value,),
                )
            if "derived field" in msg or "total_amount" in msg:
                conn.rollback()
                return _build_and_maybe_persist_blocked_result(
                    conn,
                    inp,
                    created_at,
                    (FinalMutationWorkflowBlockReason.ADJUST_FIELD_NOT_ALLOWED.value,),
                )
            if "no canonical currency" in msg:
                conn.rollback()
                return _build_and_maybe_persist_blocked_result(
                    conn,
                    inp,
                    created_at,
                    (FinalMutationWorkflowBlockReason.CREATE_MISSING_CURRENCY.value,),
                )
            raise

    audit_refs = _build_audit_refs(inp)
    mutation_payload = _build_mutation_payload(
        inp,
        validated_create_money=validated_create_money,
    )

    _insert_audit_record(
        conn,
        final_mutation_id=final_mutation_id,
        idempotency_key=inp.idempotency_key,
        fingerprint=fingerprint,
        operation_id=inp.operation_id,
        plan_id=inp.plan.plan_id,
        proposal_id=inp.proposal.proposal_id,
        guard_decision_proposal_id=inp.guard_decision.proposal_id,
        guard_version=inp.guard_decision.guard_version,
        guarded_execution_id=inp.guard_execution_result.idempotency_key,
        guarded_execution_idempotency_key=inp.guard_execution_result.idempotency_key,
        human_confirmation_id=inp.human_confirmation_id,
        actor_type=inp.actor_type,
        actor_id=inp.actor_id,
        action=action_value,
        status=FinalMutationWorkflowStatus.FINALIZED.value,
        source_statement_ref=inp.proposal.source_statement_ref,
        source_app_transaction_ref=inp.proposal.source_app_transaction_ref,
        target_transaction_id=inp.proposal.target_transaction_id,
        transaction_public_id=transaction_public_id,
        mutation_payload=mutation_payload,
        evidence_refs=inp.proposal.evidence_refs,
        audit_refs=audit_refs,
        created_at=created_at,
    )

    if previous_set:
        _store_previous_values(conn, final_mutation_id, previous_values)
    if transaction_public_id is None:
        raise FinalMutationPersistenceError("Final mutation produced no transaction identity")
    _append_final_mutation_chain_event(
        conn,
        inp=inp,
        final_mutation_id=final_mutation_id,
        fingerprint=fingerprint,
        transaction_public_id=transaction_public_id,
        previous_values=previous_values if previous_set else None,
        mutation_payload=mutation_payload,
        validated_create_money=validated_create_money,
        created_at=created_at,
    )
    conn.commit()

    return FinalMutationWorkflowResult(
        final_mutation_id=final_mutation_id,
        operation_id=inp.operation_id,
        proposal_id=inp.proposal.proposal_id,
        action=action_value,
        status=FinalMutationWorkflowStatus.FINALIZED,
        transaction_public_id=transaction_public_id,
        idempotency_key=inp.idempotency_key,
        idempotency_fingerprint=fingerprint,
        audit_refs=audit_refs,
        blocked_reasons=(),
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# CREATE_FINAL_TRANSACTION
# ---------------------------------------------------------------------------


def _execute_create(
    conn: sqlite3.Connection,
    inp: FinalMutationWorkflowInput,
    final_mutation_id: str,
    created_at: str,
    validated_money: ValidatedReconciliationCreateMoney,
) -> str:
    """Execute CREATE_FINAL_TRANSACTION: insert one canonical transaction row."""
    proposal = inp.proposal
    public_id = _generate_transaction_public_id(inp, validated_money)

    amount_str = validated_money.canonical_amount
    date_str = (
        proposal.transaction_date.isoformat() if proposal.transaction_date is not None else ""
    )

    notes_json = _serialize_json(
        {
            "proposal_note": proposal.note or "",
            "final_mutation_id": final_mutation_id,
            "source_app_transaction_ref": proposal.source_app_transaction_ref,
            "evidence_refs": sorted(proposal.evidence_refs),
            "conversion_source": "reconciliation_final_mutation",
        }
    )

    conn.execute(
        """
        INSERT INTO transactions (
            public_id, intent, intent_type,
            transaction_date, status, amount, total_amount,
            currency, merchant, category, notes,
            statement_source, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            public_id,
            "reconciliation_generated",
            "Generated",
            date_str,
            "active",
            amount_str,
            amount_str,
            validated_money.currency,
            proposal.merchant or "",
            None,
            notes_json,
            proposal.source_statement_ref,
            created_at,
        ),
    )

    return public_id


# ---------------------------------------------------------------------------
# ADJUST_FINAL_TRANSACTION
# ---------------------------------------------------------------------------


def _execute_adjust(
    conn: sqlite3.Connection,
    inp: FinalMutationWorkflowInput,
    final_mutation_id: str,
    created_at: str,
) -> tuple[str, bool, dict[str, Any]]:
    """Execute ADJUST_FINAL_TRANSACTION: update allowed fields on one row.

    Returns (transaction_public_id, previous_values_set).
    """
    proposal = inp.proposal
    target_id = proposal.target_transaction_id

    if not target_id:
        raise ValueError("ADJUST_FINAL_TRANSACTION requires target_transaction_id")

    field_errors = _validate_adjust_fields(proposal.suggested_fields)
    if field_errors:
        raise ValueError(f"ADJUST field validation failed: {'; '.join(field_errors)}")

    rows = conn.execute(
        """
        SELECT public_id, amount, total_amount, currency,
               transaction_date, merchant, category, notes
        FROM transactions
        WHERE public_id = ?
        """,
        (target_id,),
    ).fetchall()

    if len(rows) == 0:
        raise ValueError(f"ADJUST target transaction '{target_id}' not found")
    if len(rows) > 1:
        raise ValueError(f"ADJUST target transaction '{target_id}' is ambiguous ({len(rows)} rows)")

    row = rows[0]
    previous = {
        "amount": row[1],
        "total_amount": row[2],
        "currency": row[3],
        "transaction_date": row[4],
        "merchant": row[5],
        "category": row[6],
        "notes": row[7],
    }

    set_clauses: list[str] = []
    params: list[Any] = []

    new_amount_str: str | None = None

    for fname, new_value in sorted(proposal.suggested_fields.items()):
        if fname not in _ADJUST_ALLOWED_FIELDS:
            continue
        if fname == "total_amount":
            # Callers must not independently supply total_amount — it is a
            # derived field always kept equal to amount.
            raise ValueError(
                f"Field .{fname}. is a derived field and cannot be independently adjusted"
            )
        if fname == "amount":
            # Validate the original value through the Money Contract.
            # Do NOT pre-coerce floats to strings — let money_decimal reject them.
            target_currency = row[3]
            if not target_currency:
                raise ValueError(
                    "ADJUST target transaction has no canonical currency; "
                    "monetary validation cannot proceed"
                )
            amount_decimal = money_decimal(
                new_value,
                label="adjustment amount",
            )
            amount_decimal = validate_amount_for_currency(
                amount_decimal, target_currency, label="adjustment amount"
            )
            new_amount_str = canonical_decimal_str(amount_decimal)
            set_clauses.append("amount = ?")
            params.append(new_amount_str)
            set_clauses.append("total_amount = ?")
            params.append(new_amount_str)
        else:
            set_clauses.append(f"{fname} = ?")
            params.append(str(new_value) if new_value is not None else None)

    if not set_clauses:
        raise ValueError("ADJUST_FINAL_TRANSACTION has no allowed fields to update")

    params.append(target_id)
    conn.execute(
        f"UPDATE transactions SET {', '.join(set_clauses)} WHERE public_id = ?",
        params,
    )

    return target_id, True, previous


def _validate_adjust_fields(suggested_fields: dict[str, Any]) -> list[str]:
    """Validate suggested fields for ADJUST_FINAL_TRANSACTION."""
    errors: list[str] = []

    if not suggested_fields:
        errors.append("suggested_fields is empty -- no fields to update")
        return errors

    for fname in suggested_fields:
        if fname not in _ADJUST_ALLOWED_FIELDS:
            field_lower = fname.lower()
            forbidden = any(marker in field_lower for marker in _FORBIDDEN_FIELD_MARKERS)
            if forbidden:
                errors.append(f"Field .{fname}. is forbidden (contains a blocked marker)")
            else:
                errors.append(
                    f"Field .{fname}. is not in the allowed set: "
                    f"{', '.join(sorted(_ADJUST_ALLOWED_FIELDS))}"
                )

    return errors


def _store_previous_values(
    conn: sqlite3.Connection,
    final_mutation_id: str,
    previous: dict[str, Any],
) -> None:
    """Store previous values in the audit record for ADJUST traceability."""
    conn.execute(
        """
        UPDATE reconciliation_final_mutation_audit
        SET previous_values_json = ?
        WHERE final_mutation_id = ?
        """,
        (_serialize_json(previous), final_mutation_id),
    )


def _append_final_mutation_chain_event(
    conn: sqlite3.Connection,
    *,
    inp: FinalMutationWorkflowInput,
    final_mutation_id: str,
    fingerprint: str,
    transaction_public_id: str,
    previous_values: dict[str, Any] | None,
    mutation_payload: dict[str, object],
    validated_create_money: ValidatedReconciliationCreateMoney | None,
    created_at: str,
) -> None:
    action = inp.proposal.action
    event_type = (
        "reconciliation_final_transaction_created"
        if action == FinalMutationAction.CREATE_FINAL_TRANSACTION
        else "reconciliation_final_transaction_adjusted"
    )
    head = FinancialAuditRepository(conn).head("transaction", transaction_public_id)
    previous_state = (
        None
        if previous_values is None or head is not None
        else _audit_safe_transaction_state(previous_values)
    )
    new_state = _load_transaction_audit_state(conn, transaction_public_id)
    if action == FinalMutationAction.CREATE_FINAL_TRANSACTION:
        if validated_create_money is None:
            raise FinalMutationPersistenceError("Validated CREATE money is required for audit")
        _canonicalize_validated_create_audit_state(new_state, validated_create_money)
    event_id = derive_audit_event_public_id(
        aggregate_type="transaction",
        aggregate_public_id=transaction_public_id,
        event_type=event_type,
        causation_public_id=final_mutation_id,
    )
    append_financial_audit_event(
        conn,
        AuditEventCommand(
            event_public_id=event_id,
            aggregate_type="transaction",
            aggregate_public_id=transaction_public_id,
            event_type=event_type,
            event_payload={
                "final_mutation_id": final_mutation_id,
                "idempotency_fingerprint": fingerprint,
                "operation_id": inp.operation_id,
                "plan_id": inp.plan.plan_id,
                "proposal_id": inp.proposal.proposal_id,
                "action": action.value,
                "mutation_payload": mutation_payload,
                "previous_values": (
                    _audit_safe_transaction_state(previous_values)
                    if previous_values is not None
                    else None
                ),
            },
            previous_state=previous_state,
            new_state=new_state,
            actor_type=inp.actor_type,
            actor_public_id=inp.actor_id or f"reconciliation-final-mutation:{inp.actor_type}",
            authorization_public_id=inp.authorization_id,
            source_evidence_references=inp.proposal.evidence_refs,
            correlation_public_id=inp.plan.plan_id,
            causation_public_id=final_mutation_id,
            created_at=created_at,
        ),
    )


def _load_transaction_audit_state(
    conn: sqlite3.Connection,
    transaction_public_id: str,
) -> dict[str, object]:
    row = conn.execute(
        """SELECT public_id, intent, intent_type, transaction_date, status,
        CAST(amount AS TEXT) AS amount, CAST(total_amount AS TEXT) AS total_amount,
        currency, merchant, category, notes, statement_source
        FROM transactions WHERE public_id = ?""",
        (transaction_public_id,),
    ).fetchone()
    if row is None:
        raise FinalMutationPersistenceError("Audited transaction is missing")
    return {key: row[key] for key in row.keys()}


def _audit_safe_transaction_state(values: dict[str, Any]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values.items():
        if key in {"amount", "total_amount"} and value is not None:
            result[key] = str(value)
        else:
            result[key] = value
    return result


def _canonicalize_validated_create_audit_state(
    state: dict[str, object],
    validated_money: ValidatedReconciliationCreateMoney,
) -> None:
    """Verify persisted CREATE money, then expose its canonical audit text."""
    try:
        stored_amount = money_decimal(
            state.get("amount"),
            label="persisted reconciliation CREATE amount",
        )
        stored_total = money_decimal(
            state.get("total_amount"),
            label="persisted reconciliation CREATE total_amount",
        )
    except MoneyValidationError as exc:
        raise FinalMutationPersistenceError("Persisted CREATE money is malformed") from exc
    if (
        stored_amount != validated_money.amount
        or stored_total != validated_money.amount
        or state.get("currency") != validated_money.currency
    ):
        raise FinalMutationPersistenceError(
            "Persisted CREATE money does not match the authorized canonical value"
        )
    state["amount"] = validated_money.canonical_amount
    state["total_amount"] = validated_money.canonical_amount
    state["currency"] = validated_money.currency


# ---------------------------------------------------------------------------
# Idempotency fingerprint builder
# ---------------------------------------------------------------------------


def _build_final_mutation_fingerprint(
    inp: FinalMutationWorkflowInput,
    *,
    validated_create_money: ValidatedReconciliationCreateMoney | None = None,
) -> str:
    """Build a deterministic SHA-256 fingerprint for idempotency."""
    payload = _build_mutation_payload(
        inp,
        validated_create_money=validated_create_money,
    )
    canonical = json.dumps(
        {
            "version": (
                _CURRENT_CREATE_FINGERPRINT_VERSION
                if inp.proposal.action == FinalMutationAction.CREATE_FINAL_TRANSACTION
                else _LEGACY_FINGERPRINT_VERSION
            ),
            "operation_id": inp.operation_id,
            "proposal_id": inp.proposal.proposal_id,
            "guard_version": inp.guard_decision.guard_version,
            "human_confirmation_id": inp.human_confirmation_id,
            "action": inp.proposal.action.value,
            "mutation_payload": payload,
            "evidence_refs": sorted(inp.proposal.evidence_refs),
            "idempotency_key": inp.idempotency_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_mutation_payload(
    inp: FinalMutationWorkflowInput,
    *,
    validated_create_money: ValidatedReconciliationCreateMoney | None = None,
) -> dict[str, object]:
    """Build a deterministic mutation payload dict from the proposal."""
    proposal = inp.proposal
    payload: dict[str, object] = {}

    if proposal.action == FinalMutationAction.CREATE_FINAL_TRANSACTION:
        if validated_create_money is None:
            try:
                validated_create_money = validate_reconciliation_create_money(
                    proposal.amount,
                    proposal.currency,
                )
            except CreateMoneyValidationError as exc:
                payload["money_validation"] = [issue.value for issue in exc.issues]
        if validated_create_money is not None:
            payload["amount"] = validated_create_money.canonical_amount
            payload["currency"] = validated_create_money.currency
    else:
        if proposal.amount is not None:
            payload["amount"] = str(proposal.amount)
        if proposal.currency:
            payload["currency"] = proposal.currency
    if proposal.merchant:
        payload["merchant"] = proposal.merchant
    if proposal.transaction_date is not None:
        payload["transaction_date"] = proposal.transaction_date.isoformat()
    if proposal.target_transaction_id:
        payload["target_transaction_id"] = proposal.target_transaction_id
    if proposal.suggested_fields:
        payload["suggested_fields"] = dict(sorted(proposal.suggested_fields.items()))
    if proposal.source_statement_ref:
        payload["source_statement_ref"] = proposal.source_statement_ref
    if proposal.source_app_transaction_ref:
        payload["source_app_transaction_ref"] = proposal.source_app_transaction_ref
    if proposal.note:
        payload["note"] = proposal.note

    return payload


def _build_legacy_v1_mutation_payload(
    inp: FinalMutationWorkflowInput,
) -> dict[str, object]:
    """Exactly reproduce the pre-209 payload for verifier-only compatibility."""
    proposal = inp.proposal
    payload: dict[str, object] = {}
    if proposal.amount is not None:
        payload["amount"] = str(proposal.amount)
    if proposal.currency:
        payload["currency"] = proposal.currency
    if proposal.merchant:
        payload["merchant"] = proposal.merchant
    if proposal.transaction_date is not None:
        payload["transaction_date"] = proposal.transaction_date.isoformat()
    if proposal.target_transaction_id:
        payload["target_transaction_id"] = proposal.target_transaction_id
    if proposal.suggested_fields:
        payload["suggested_fields"] = dict(sorted(proposal.suggested_fields.items()))
    if proposal.source_statement_ref:
        payload["source_statement_ref"] = proposal.source_statement_ref
    if proposal.source_app_transaction_ref:
        payload["source_app_transaction_ref"] = proposal.source_app_transaction_ref
    if proposal.note:
        payload["note"] = proposal.note
    return payload


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


def _build_audit_refs(inp: FinalMutationWorkflowInput) -> dict[str, object]:
    """Build the audit refs dict for the result and persistence."""
    return {
        "plan_id": inp.plan.plan_id,
        "proposal_id": inp.proposal.proposal_id,
        "guard_decision_proposal_id": inp.guard_decision.proposal_id,
        "guard_version": inp.guard_decision.guard_version,
        "guard_decision_approved": inp.guard_decision.approved,
        "guarded_execution_idempotency_key": inp.guard_execution_result.idempotency_key,
        "guarded_execution_status": inp.guard_execution_result.execution_status.value,
        "human_confirmation_id": inp.human_confirmation_id,
        "persisted_authorization_id": inp.authorization_id,
        "evidence_refs": list(inp.proposal.evidence_refs),
        "workflow_version": "v1",
    }


def _insert_audit_record(
    conn: sqlite3.Connection,
    *,
    final_mutation_id: str,
    idempotency_key: str,
    fingerprint: str,
    operation_id: str,
    plan_id: str,
    proposal_id: str,
    guard_decision_proposal_id: str,
    guard_version: str,
    guarded_execution_id: str,
    guarded_execution_idempotency_key: str,
    human_confirmation_id: str,
    actor_type: str,
    actor_id: str | None,
    action: str,
    status: str,
    source_statement_ref: str | None,
    source_app_transaction_ref: str | None,
    target_transaction_id: str | None,
    transaction_public_id: str | None,
    mutation_payload: dict[str, object],
    evidence_refs: tuple[str, ...],
    audit_refs: dict[str, object],
    created_at: str,
    blocked_reasons: tuple[str, ...] = (),
) -> None:
    """Insert a final mutation audit record."""
    conn.execute(
        """
        INSERT INTO reconciliation_final_mutation_audit (
            final_mutation_id, idempotency_key, idempotency_fingerprint,
            operation_id, plan_id, proposal_id,
            guard_decision_proposal_id, guard_version,
            guarded_execution_id, guarded_execution_idempotency_key,
            human_confirmation_id, actor_type, actor_id,
            action, status, blocked_reasons_json,
            source_statement_ref, source_app_transaction_ref,
            target_transaction_id, transaction_public_id,
            mutation_payload_json, evidence_refs_json,
            audit_refs_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            final_mutation_id,
            idempotency_key,
            fingerprint,
            operation_id,
            plan_id,
            proposal_id,
            guard_decision_proposal_id,
            guard_version,
            guarded_execution_id,
            guarded_execution_idempotency_key,
            human_confirmation_id,
            actor_type,
            actor_id,
            action,
            status,
            _serialize_json(list(blocked_reasons)),
            source_statement_ref,
            source_app_transaction_ref,
            target_transaction_id,
            transaction_public_id,
            _serialize_json(mutation_payload),
            _serialize_json(list(evidence_refs)),
            _serialize_json(audit_refs),
            created_at,
        ),
    )


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------


def _build_blocked_result(
    inp: FinalMutationWorkflowInput,
    created_at: str,
    block_reasons: tuple[str, ...],
) -> FinalMutationWorkflowResult:
    """Build a blocked result with stable reason codes."""
    final_mutation_id = (
        _derive_final_mutation_id(inp.idempotency_key)
        if inp.idempotency_key
        else "fm-blocked-no-key"
    )
    fingerprint = _build_final_mutation_fingerprint(inp) if inp.idempotency_key else ""
    return FinalMutationWorkflowResult(
        final_mutation_id=final_mutation_id,
        operation_id=inp.operation_id,
        proposal_id=inp.proposal.proposal_id,
        action=inp.proposal.action.value,
        status=FinalMutationWorkflowStatus.BLOCKED,
        transaction_public_id=None,
        idempotency_key=inp.idempotency_key,
        idempotency_fingerprint=fingerprint,
        audit_refs=_build_audit_refs(inp),
        blocked_reasons=block_reasons,
        created_at=created_at,
    )


def _build_and_maybe_persist_blocked_result(
    conn: sqlite3.Connection,
    inp: FinalMutationWorkflowInput,
    created_at: str,
    block_reasons: tuple[str, ...],
) -> FinalMutationWorkflowResult:
    """Build a blocked result and persist it when audit persistence exists."""
    if conn.in_transaction:
        raise FinalMutationTransactionError(
            "Blocked final-mutation audit persistence requires a connection without pending work."
        )
    result = _build_blocked_result(inp, created_at, block_reasons)

    if not inp.idempotency_key or not _table_exists(conn, "reconciliation_final_mutation_audit"):
        return result

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            """
            SELECT 1
            FROM reconciliation_final_mutation_audit
            WHERE idempotency_key = ?
            """,
            (inp.idempotency_key,),
        ).fetchone()
        if existing is not None:
            conn.rollback()
            return result

        _insert_audit_record(
            conn,
            final_mutation_id=result.final_mutation_id,
            idempotency_key=inp.idempotency_key,
            fingerprint=result.idempotency_fingerprint,
            operation_id=inp.operation_id,
            plan_id=inp.plan.plan_id,
            proposal_id=inp.proposal.proposal_id,
            guard_decision_proposal_id=inp.guard_decision.proposal_id,
            guard_version=inp.guard_decision.guard_version,
            guarded_execution_id=inp.guard_execution_result.idempotency_key,
            guarded_execution_idempotency_key=inp.guard_execution_result.idempotency_key,
            human_confirmation_id=inp.human_confirmation_id,
            actor_type=inp.actor_type,
            actor_id=inp.actor_id,
            action=inp.proposal.action.value,
            status=FinalMutationWorkflowStatus.BLOCKED.value,
            source_statement_ref=inp.proposal.source_statement_ref,
            source_app_transaction_ref=inp.proposal.source_app_transaction_ref,
            target_transaction_id=inp.proposal.target_transaction_id,
            transaction_public_id=None,
            mutation_payload=_build_mutation_payload(inp),
            evidence_refs=inp.proposal.evidence_refs,
            audit_refs=result.audit_refs,
            created_at=created_at,
            blocked_reasons=block_reasons,
        )
        conn.commit()
        return result
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Table setup
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Return whether a table exists in the caller-supplied connection."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------


def _derive_final_mutation_id(idempotency_key: str) -> str:
    """Derive a stable final_mutation_id from the idempotency key."""
    digest = hashlib.sha256(f"fm-v1|{idempotency_key}".encode("utf-8")).hexdigest()
    return f"fm-{digest[:16]}"


def _generate_transaction_public_id(
    inp: FinalMutationWorkflowInput,
    validated_money: ValidatedReconciliationCreateMoney,
) -> str:
    """Generate a stable, unique transaction public_id.

    Derived from proposal ID, operation ID, human confirmation ID,
    the idempotency key, and material transaction fields.  Including
    the idempotency key ensures that two identical proposals with
    different final mutation keys produce distinct public IDs.
    """
    proposal = inp.proposal
    parts: list[str] = [
        proposal.proposal_id,
        inp.operation_id,
        inp.human_confirmation_id,
        inp.idempotency_key,
        validated_money.canonical_amount,
        validated_money.currency,
        proposal.transaction_date.isoformat() if proposal.transaction_date is not None else "",
        proposal.merchant or "",
    ]
    raw = "|".join(parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"txn-fm-{digest[:16]}"


# ---------------------------------------------------------------------------
# JSON serialization (deterministic)
# ---------------------------------------------------------------------------


def _serialize_json(value: object) -> str:
    """Serialize a value to deterministic JSON."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _reject_non_finite_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON constant is not allowed: {value}")


def _strict_json_load(raw: object) -> object:
    if not isinstance(raw, str):
        raise TypeError("Persisted JSON material must be text")
    return json.loads(raw, parse_constant=_reject_non_finite_json_constant)


def _strict_json_dict(raw: object) -> dict[str, object]:
    loaded = _strict_json_load(raw)
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        raise ValueError("Persisted JSON material must be an object with string keys")
    return loaded


def _strict_json_string_list(raw: object) -> list[str]:
    loaded = _strict_json_load(raw)
    if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
        raise ValueError("Persisted JSON material must be a list of strings")
    return loaded


def _deserialize_json_dict(raw: str | None) -> dict[str, object]:
    """Deserialize a JSON string to a dict."""
    if not raw:
        return {}
    loaded = json.loads(raw)
    if isinstance(loaded, dict):
        return loaded
    return {}


def _deserialize_json_list(raw: str | None) -> list[str]:
    """Deserialize a JSON string to a string list."""
    if not raw:
        return []
    loaded = json.loads(raw)
    if isinstance(loaded, list):
        return [str(item) for item in loaded]
    return []


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------

__all__ = [
    "FinalMutationPersistenceError",
    "FinalMutationTransactionError",
    "FinalMutationWorkflowStatus",
    "FinalMutationWorkflowBlockReason",
    "FinalMutationWorkflowInput",
    "FinalMutationWorkflowResult",
    "execute_guarded_final_mutation_workflow",
]
