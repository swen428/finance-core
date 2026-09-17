"""Reconciliation Resolution Apply Runtime v1 -- controlled, audit-safe
conversion of human resolution decisions into explicit apply results.

The ``ResolutionApplyRuntime`` is a stateful in-memory layer that:

- Validates decision/issue compatibility.
- Enforces idempotency and conflict rules.
- Builds action-specific payloads (proposals for mutations, confirmations
  for safe actions).
- Tracks applied decisions to prevent silent overwrites.
- Never silently mutates final financial records.

Design properties:
- In-memory only in v1 (no SQLite writes).
- Deterministic: same inputs produce the same output.
- Audit-friendly: every ``ResolutionApplyResult`` carries structured evidence.
- Action-specific payloads distinguish between safe confirmations and
  guarded proposals for future mutation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from finance_core.reconciliation.models import (
    ApplyConflictError,
    ApplyInstruction,
    ApplyInstructionError,
    BatchApplyResult,
    BatchApplyState,
    IssueType,
    ResolutionAction,
    ResolutionApplyResult,
    ResolutionDecision,
    ReviewQueueItem,
    validate_resolution_decision,
)

# ---------------------------------------------------------------------------
# Required reference fields per action
# ---------------------------------------------------------------------------

_ACTION_REQUIRED_REFERENCES: dict[ResolutionAction, tuple[str, ...]] = {
    ResolutionAction.CONFIRM_MATCH: ("best_app_transaction",),
    ResolutionAction.MARK_DUPLICATE: ("all_app_transactions",),
    ResolutionAction.CREATE_MISSING_APP_TRANSACTION: ("statement",),
    ResolutionAction.ADJUST_APP_TRANSACTION: ("best_app_transaction",),
    ResolutionAction.MARK_STATEMENT_ONLY: ("statement",),
    ResolutionAction.IGNORE: (),
    ResolutionAction.NEEDS_MORE_INFO: (),
}
"""Required candidate fields for each resolution action.

The apply runtime checks that the queue item's candidate has a non-None
value for each listed field before accepting the decision.
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Apply Instruction Builder -- explicit review → apply boundary
# ---------------------------------------------------------------------------


def build_apply_instruction(
    item: ReviewQueueItem,
    decision: ResolutionDecision,
    *,
    evidence_refs: tuple[str, ...] = (),
    require_evidence_ref: bool = False,
) -> ApplyInstruction:
    """Build a validated ``ApplyInstruction`` from review data.

    This is the explicit boundary between the review/evidence layer and
    the apply runtime.  Every apply operation must pass through this
    function (or an equivalent validated instruction builder) before
    the apply runtime can act.

    Validation rules enforced here:
    1. ``decision.queue_item_id`` must match ``item.queue_item_id``.
    2. ``item.candidate.candidate_id`` must not be empty (ambiguous identity).
    3. Action must be compatible with the issue type.
    4. Required candidate references must be present.
    5. Evidence reference may be required via ``require_evidence_ref``.
    6. Monetary amount must be present for amount-aware actions.

    Returns:
        A validated ``ApplyInstruction``.

    Raises:
        ApplyInstructionError: When any validation rule fails.
    """
    # -- Rule 1: queue_item_id match --
    if decision.queue_item_id != item.queue_item_id:
        raise ApplyInstructionError(
            f"Decision queue_item_id '{decision.queue_item_id}' does not match "
            f"item queue_item_id '{item.queue_item_id}'"
        )

    # -- Rule 2: candidate identity must be unambiguous --
    if not item.candidate.candidate_id:
        raise ApplyInstructionError(
            f"Candidate identity is ambiguous (empty candidate_id) for "
            f"queue item '{item.queue_item_id}'. Cannot build apply instruction."
        )

    # -- Rule 3: action/issue compatibility --
    is_compat, compat_error = validate_resolution_decision(decision.action, item.issue_type)
    if not is_compat:
        raise ApplyInstructionError(
            compat_error
            or f"Action '{decision.action.value}' incompatible with "
            f"issue type '{item.issue_type.value}'"
        )

    # -- Rule 4: required candidate references --
    ref_error = _check_instruction_references(item, decision)
    if ref_error is not None:
        raise ApplyInstructionError(ref_error)

    # -- Rule 5: evidence reference (optional enforcement) --
    if require_evidence_ref and not evidence_refs:
        raise ApplyInstructionError(
            f"Evidence reference is required for apply instruction on "
            f"queue item '{item.queue_item_id}' but none provided."
        )

    # -- Rule 6: monetary amount for amount-aware actions --
    _check_monetary_amount(item, decision)

    # -- Build instruction --
    instruction_id = f"instr-{decision.decision_id}"
    cand = item.candidate

    return ApplyInstruction(
        instruction_id=instruction_id,
        decision_id=decision.decision_id,
        queue_item_id=item.queue_item_id,
        candidate_id=cand.candidate_id,
        action=decision.action,
        reviewer=decision.reviewer,
        note=decision.note,
        resolved_at=decision.resolved_at,
        issue_type=item.issue_type,
        statement_ref=_statement_ref_from_item(item),
        app_transaction_ref=_app_ref_from_item(item),
        evidence_refs=evidence_refs,
        audit_metadata={
            "build_version": "v1",
            "source": "build_apply_instruction",
            "instruction_id": instruction_id,
        },
        _item=item,
    )


def _check_instruction_references(
    item: ReviewQueueItem,
    decision: ResolutionDecision,
) -> str | None:
    """Check that the queue item's candidate has all required references
    for the given action.  Returns an error message or None."""
    required = _ACTION_REQUIRED_REFERENCES.get(decision.action, ())
    if not required:
        return None

    cand = item.candidate
    for field_name in required:
        value = getattr(cand, field_name, None)
        if value is None:
            return (
                f"Action '{decision.action.value}' requires candidate field "
                f"'{field_name}' but it is None for candidate "
                f"'{cand.candidate_id}'"
            )
        if field_name == "all_app_transactions" and len(value) < 2:
            return (
                f"Action '{decision.action.value}' requires at least 2 "
                f"app transactions for duplicate detection, but candidate "
                f"'{cand.candidate_id}' has {len(value)}"
            )
    return None


def _check_monetary_amount(
    item: ReviewQueueItem,
    decision: ResolutionDecision,
) -> None:
    """Raise ApplyInstructionError when a monetary-amount-aware action
    is requested but the statement amount is missing."""
    amount_aware_actions = {
        ResolutionAction.ADJUST_APP_TRANSACTION,
        ResolutionAction.CREATE_MISSING_APP_TRANSACTION,
    }
    if decision.action not in amount_aware_actions:
        return

    stmt = item.candidate.statement
    if stmt.amount is None:
        raise ApplyInstructionError(
            f"Action '{decision.action.value}' requires a statement amount "
            f"but statement amount is None for candidate "
            f"'{item.candidate.candidate_id}'"
        )


def _statement_ref_from_item(item: ReviewQueueItem) -> str | None:
    """Extract a stable statement reference from a review queue item."""
    stmt = item.candidate.statement
    if stmt is None:
        return None
    return stmt.statement_row_reference or stmt.merchant_raw


def _app_ref_from_item(item: ReviewQueueItem) -> str | None:
    """Extract a stable app transaction reference from a review queue item."""
    app = item.candidate.best_app_transaction
    if app is None:
        return None
    return app.app_txn_id


class ResolutionApplyRuntime:
    """Stateful apply runtime that converts resolution decisions into
    auditable apply results.

    Maintains an in-memory registry of applied decisions to enforce
    idempotency and prevent conflicts.

    Usage::

        runtime = ResolutionApplyRuntime()
        decision = ResolutionDecision(
            decision_id="dec-001",
            queue_item_id="q-000",
            action=ResolutionAction.CONFIRM_MATCH,
            note="Looks correct.",
        )
        result = runtime.apply(item, decision)
        assert result.success

    Idempotency:
        Re-applying the exact same decision (same decision_id, queue_item_id,
        action, note, reviewer) is idempotent and returns success with
        ``idempotent=True``.

    Conflicts:
        Applying a decision with a ``decision_id`` already used for a different
        queue item, or applying a different decision to an already-resolved
        queue item, raises ``ApplyConflictError``.
    """

    def __init__(self) -> None:
        # Registry: decision_id ->
        #   (queue_item_id, action, fingerprint, applied_at, audit_evidence)
        self._applied: dict[str, tuple[str, ResolutionAction, str, str, dict]] = {}
        self._item_to_decision: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Main apply entry point
    # ------------------------------------------------------------------

    def apply(
        self,
        item: ReviewQueueItem,
        decision: ResolutionDecision,
        *,
        evidence_refs: tuple[str, ...] = (),
        require_evidence_ref: bool = False,
    ) -> ResolutionApplyResult:
        """Apply a resolution decision to a review queue item.

        Internally builds an ``ApplyInstruction`` via
        ``build_apply_instruction()`` — the explicit review→apply boundary —
        then delegates to ``apply_instruction()``.

        Returns a ``ResolutionApplyResult`` on success or a controlled error
        result on validation failure. Raises ``ApplyConflictError`` for
        conflicting repeat applications.
        """
        # -- Build apply instruction (enforces review→apply boundary) --
        try:
            instruction = build_apply_instruction(
                item,
                decision,
                evidence_refs=evidence_refs,
                require_evidence_ref=require_evidence_ref,
            )
        except ApplyInstructionError as e:
            now_iso = datetime.now(timezone.utc).isoformat()
            return ResolutionApplyResult(
                apply_id=f"apply-{decision.decision_id}",
                decision_id=decision.decision_id,
                queue_item_id=decision.queue_item_id,
                candidate_id=item.candidate.candidate_id,
                action=decision.action,
                success=False,
                error_message=str(e),
                audit_evidence={
                    "validation_error": str(e),
                    "action": decision.action.value,
                    "issue_type": item.issue_type.value,
                    "queue_item_id": item.queue_item_id,
                    "decision_id": decision.decision_id,
                    "candidate_id": item.candidate.candidate_id,
                    "evidence_refs": list(evidence_refs),
                    "require_evidence_ref": require_evidence_ref,
                },
                applied_at=now_iso,
                reviewer=decision.reviewer,
                note=decision.note,
            )

        return self.apply_instruction(instruction)

    def apply_instruction(
        self,
        instruction: ApplyInstruction,
    ) -> ResolutionApplyResult:
        """Apply a validated ``ApplyInstruction``.

        This is the primary entry point for the apply runtime.
        It accepts only ``ApplyInstruction`` objects that have passed
        the ``build_apply_instruction()`` validation boundary.

        Enforces idempotency and conflict rules, builds action-specific
        payloads, and records the application in the in-memory registry.
        """

        # -- Runtime type guard: only ApplyInstruction objects are accepted --
        if not isinstance(instruction, ApplyInstruction):
            raise ApplyInstructionError(
                "apply_instruction() only accepts ApplyInstruction objects. "
                "All apply input must pass through build_apply_instruction(). "
                f"Received '{type(instruction).__name__}' instead."
            )
        now_iso = datetime.now(timezone.utc).isoformat()

        # -- Idempotency check --
        idem_result = self._check_idempotency_instruction(instruction)
        if idem_result is not None:
            return idem_result

        # -- Conflict check --
        self._check_conflict_instruction(instruction)

        # -- Build action-specific payload --
        payload = self._build_payload_instruction(instruction)
        audit_evidence = self._build_audit_evidence_instruction(instruction, now_iso)
        fingerprint = self._build_fingerprint_instruction(instruction, payload)

        result = ResolutionApplyResult(
            apply_id=f"apply-{instruction.decision_id}",
            decision_id=instruction.decision_id,
            queue_item_id=instruction.queue_item_id,
            candidate_id=instruction.candidate_id,
            action=instruction.action,
            success=True,
            payload=payload,
            audit_evidence=audit_evidence,
            applied_at=now_iso,
            reviewer=instruction.reviewer,
            note=instruction.note,
            statement_reference=instruction.statement_ref,
            app_transaction_reference=instruction.app_transaction_ref,
            idempotent=False,
        )

        # -- Record --
        self._applied[instruction.decision_id] = (
            instruction.queue_item_id,
            instruction.action,
            fingerprint,
            now_iso,
            audit_evidence,
        )
        self._item_to_decision[instruction.queue_item_id] = instruction.decision_id

        return result

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def _check_idempotency_instruction(
        self,
        instruction: ApplyInstruction,
    ) -> ResolutionApplyResult | None:
        """Return an idempotent success result if the decision was already
        applied identically, or None if this is a new application."""
        if instruction.decision_id not in self._applied:
            return None

        (
            prev_qid,
            prev_action,
            prev_fingerprint,
            prev_applied_at,
            prev_evidence,
        ) = self._applied[instruction.decision_id]

        # Decision ID already used for a different queue item -> conflict
        if prev_qid != instruction.queue_item_id:
            raise ApplyConflictError(
                f"Decision '{instruction.decision_id}' was already applied to "
                f"queue item '{prev_qid}', cannot apply to '{instruction.queue_item_id}'"
            )

        # Compute current fingerprint to compare
        payload = self._build_payload_instruction(instruction)
        cur_fingerprint = self._build_fingerprint_instruction(instruction, payload)

        if prev_action != instruction.action:
            raise ApplyConflictError(
                f"Decision '{instruction.decision_id}' was applied with action "
                f"'{prev_action.value}', cannot re-apply with '{instruction.action.value}'"
            )

        if prev_fingerprint != cur_fingerprint:
            raise ApplyConflictError(
                f"Decision '{instruction.decision_id}' payload/evidence changed "
                f"since first application"
            )

        # Idempotent replay: reuse stable applied_at and audit_evidence
        # from the original application.
        return ResolutionApplyResult(
            apply_id=f"apply-{instruction.decision_id}",
            decision_id=instruction.decision_id,
            queue_item_id=instruction.queue_item_id,
            candidate_id=instruction.candidate_id,
            action=instruction.action,
            success=True,
            payload=payload,
            audit_evidence=prev_evidence,
            applied_at=prev_applied_at,
            reviewer=instruction.reviewer,
            note=instruction.note,
            statement_reference=instruction.statement_ref,
            app_transaction_reference=instruction.app_transaction_ref,
            idempotent=True,
        )

    def _check_conflict_instruction(
        self,
        instruction: ApplyInstruction,
    ) -> None:
        """Raise ApplyConflictError if the queue item was already resolved
        by a different decision."""
        existing_decision_id = self._item_to_decision.get(instruction.queue_item_id)
        if existing_decision_id is None:
            return
        if existing_decision_id == instruction.decision_id:
            return

        existing_action = self._applied[existing_decision_id][1]
        raise ApplyConflictError(
            f"Queue item '{instruction.queue_item_id}' was already resolved by "
            f"decision '{existing_decision_id}' (action='{existing_action.value}'). "
            f"Cannot apply decision '{instruction.decision_id}' "
            f"(action='{instruction.action.value}')"
        )

    # ------------------------------------------------------------------
    # Required reference checks
    # ------------------------------------------------------------------

    def _check_references(
        self,
        item: ReviewQueueItem,
        decision: ResolutionDecision,
    ) -> str | None:
        """Check that the queue item's candidate has all required references
        for the given action.

        Returns an error message string or None if all checks pass.
        """
        required = _ACTION_REQUIRED_REFERENCES.get(decision.action, ())
        if not required:
            return None

        cand = item.candidate
        for field_name in required:
            value = getattr(cand, field_name, None)
            if value is None:
                return (
                    f"Action '{decision.action.value}' requires candidate field "
                    f"'{field_name}' but it is None for candidate "
                    f"'{cand.candidate_id}'"
                )
            # For all_app_transactions, reject empty tuples
            if field_name == "all_app_transactions" and len(value) < 2:
                return (
                    f"Action '{decision.action.value}' requires at least 2 "
                    f"app transactions for duplicate detection, but candidate "
                    f"'{cand.candidate_id}' has {len(value)}"
                )
        return None

    # ------------------------------------------------------------------
    # Action-specific payload builders
    # ------------------------------------------------------------------

    def _build_payload_instruction(
        self,
        instruction: ApplyInstruction,
    ) -> dict[str, Any]:
        """Build an action-specific payload for the apply result."""
        item = instruction._item
        if item is None:
            return {}

        cand = item.candidate

        if instruction.action == ResolutionAction.CONFIRM_MATCH:
            return self._payload_confirm_match(cand)

        elif instruction.action == ResolutionAction.MARK_DUPLICATE:
            return self._payload_mark_duplicate(cand)

        elif instruction.action == ResolutionAction.MARK_STATEMENT_ONLY:
            return self._payload_mark_statement_only(cand)

        elif instruction.action == ResolutionAction.CREATE_MISSING_APP_TRANSACTION:
            return self._payload_create_missing_app(cand)

        elif instruction.action == ResolutionAction.ADJUST_APP_TRANSACTION:
            return self._payload_adjust_app(cand)

        elif instruction.action == ResolutionAction.IGNORE:
            return self._payload_ignore(instruction)

        elif instruction.action == ResolutionAction.NEEDS_MORE_INFO:
            return self._payload_needs_more_info(instruction)

        return {}

    # -- Individual payload builders --

    @staticmethod
    def _payload_confirm_match(cand) -> dict[str, Any]:
        app = cand.best_app_transaction
        payload: dict[str, Any] = {
            "action_type": "confirm_match",
            "statement_merchant": cand.statement.merchant_raw,
            "statement_amount": str(cand.statement.amount) if cand.statement.amount else None,
            "statement_currency": cand.statement.currency,
        }
        if cand.statement.transaction_date:
            payload["statement_txn_date"] = cand.statement.transaction_date.isoformat()
        if app is not None:
            payload["app_txn_id"] = app.app_txn_id
            payload["app_merchant"] = app.merchant
            payload["app_amount"] = str(app.amount)
            payload["app_currency"] = app.currency
            payload["app_txn_date"] = app.transaction_date.isoformat()
        payload["confidence_score"] = str(cand.confidence_score)
        payload["reason_codes"] = [r.value for r in cand.reason_codes]
        return payload

    @staticmethod
    def _payload_mark_duplicate(cand) -> dict[str, Any]:
        app_ids = [a.app_txn_id for a in cand.all_app_transactions]
        kept = cand.best_app_transaction.app_txn_id if cand.best_app_transaction else None
        return {
            "action_type": "mark_duplicate",
            "duplicate_app_txn_ids": app_ids,
            "kept_app_txn_id": kept,
            "statement_merchant": cand.statement.merchant_raw,
            "statement_amount": str(cand.statement.amount) if cand.statement.amount else None,
            "confidence_score": str(cand.confidence_score),
            "audit_only": True,
            "note": "Duplicate classification recorded. No transactions deleted or merged.",
        }

    @staticmethod
    def _payload_mark_statement_only(cand) -> dict[str, Any]:
        return {
            "action_type": "mark_statement_only",
            "statement_merchant": cand.statement.merchant_raw,
            "statement_amount": str(cand.statement.amount) if cand.statement.amount else None,
            "statement_currency": cand.statement.currency,
            "statement_row_reference": cand.statement.statement_row_reference,
            "intent": "Statement row intentionally not linked to an app transaction.",
        }

    @staticmethod
    def _payload_create_missing_app(cand) -> dict[str, Any]:
        return {
            "action_type": "proposal",
            "proposal_type": "create_missing_app_transaction",
            "statement_merchant": cand.statement.merchant_raw,
            "suggested_amount": str(cand.statement.amount) if cand.statement.amount else None,
            "suggested_currency": cand.statement.currency,
            "suggested_date": (
                cand.statement.transaction_date.isoformat()
                if cand.statement.transaction_date
                else None
            ),
            "statement_row_reference": cand.statement.statement_row_reference,
            "source_evidence": {
                "statement_merchant": cand.statement.merchant_raw,
                "statement_amount": str(cand.statement.amount) if cand.statement.amount else None,
                "statement_currency": cand.statement.currency,
                "statement_txn_date": (
                    cand.statement.transaction_date.isoformat()
                    if cand.statement.transaction_date
                    else None
                ),
            },
            "note": (
                "Proposal only. Actual transaction creation requires a guarded "
                "conversion step in a future version."
            ),
        }

    @staticmethod
    def _payload_adjust_app(cand) -> dict[str, Any]:
        app = cand.best_app_transaction
        field_adjustments: dict[str, str] = {}

        if cand.issue_type == IssueType.AMOUNT_MISMATCH:
            if cand.statement.amount is not None:
                field_adjustments["amount"] = str(cand.statement.amount)
        if cand.issue_type == IssueType.CURRENCY_MISMATCH:
            if cand.statement.currency is not None:
                field_adjustments["currency"] = cand.statement.currency
        if cand.issue_type == IssueType.DATE_MISMATCH:
            if cand.statement.transaction_date:
                field_adjustments["transaction_date"] = cand.statement.transaction_date.isoformat()
        if cand.issue_type == IssueType.MERCHANT_MISMATCH:
            field_adjustments["merchant"] = cand.statement.merchant_raw

        return {
            "action_type": "proposal",
            "proposal_type": "adjust_app_transaction",
            "app_txn_id": app.app_txn_id if app else None,
            "field_adjustments": field_adjustments,
            "statement_evidence": {
                "merchant": cand.statement.merchant_raw,
                "amount": str(cand.statement.amount) if cand.statement.amount else None,
                "currency": cand.statement.currency,
                "txn_date": (
                    cand.statement.transaction_date.isoformat()
                    if cand.statement.transaction_date
                    else None
                ),
            },
            "note": (
                "Proposal only. Actual transaction adjustment requires a guarded "
                "mutation step in a future version."
            ),
        }

    @staticmethod
    def _payload_ignore(instruction: ApplyInstruction) -> dict[str, Any]:
        return {
            "action_type": "ignore",
            "skipped": True,
            "reason": instruction.note or "(no note provided)",
        }

    @staticmethod
    def _payload_needs_more_info(instruction: ApplyInstruction) -> dict[str, Any]:
        return {
            "action_type": "needs_more_info",
            "reason": instruction.note or "(no note provided)",
            "status": "pending_investigation",
        }

    # ------------------------------------------------------------------
    # Audit evidence
    # ------------------------------------------------------------------

    def _build_audit_evidence_instruction(
        self,
        instruction: ApplyInstruction,
        now_iso: str = "",
    ) -> dict[str, Any]:
        """Build a deterministic audit evidence dict for the apply result.

        When ``now_iso`` is empty (default), a fresh UTC timestamp is used."""
        if not now_iso:
            now_iso = datetime.now(timezone.utc).isoformat()
        item = instruction._item
        if item is None:
            return {
                "apply_runtime_version": "v1",
                "resolution_action": instruction.action.value,
                "instruction_id": instruction.instruction_id,
                "error": "no review queue item associated with instruction",
            }

        cand = item.candidate

        evidence: dict[str, Any] = {
            "apply_runtime_version": "v1",
            "instruction_id": instruction.instruction_id,
            "resolution_action": instruction.action.value,
            "issue_type": item.issue_type.value,
            "reviewer": instruction.reviewer,
            "note": instruction.note,
            "applied_timestamp": instruction.resolved_at or now_iso,
            "queue_item_id": item.queue_item_id,
            "decision_id": instruction.decision_id,
            "candidate_id": cand.candidate_id,
            "confidence_score": str(cand.confidence_score),
            "reason_codes": [r.value for r in cand.reason_codes],
            "evidence_summary": item.evidence_summary,
            "evidence_refs": list(instruction.evidence_refs),
        }

        # Statement evidence
        evidence["statement_merchant"] = cand.statement.merchant_raw
        evidence["statement_amount"] = (
            str(cand.statement.amount) if cand.statement.amount is not None else None
        )
        evidence["statement_currency"] = cand.statement.currency
        if cand.statement.transaction_date:
            evidence["statement_txn_date"] = cand.statement.transaction_date.isoformat()
        if cand.statement.posted_date:
            evidence["statement_posted_date"] = cand.statement.posted_date.isoformat()

        # App transaction evidence
        if cand.best_app_transaction is not None:
            app = cand.best_app_transaction
            evidence["app_txn_id"] = app.app_txn_id
            evidence["app_merchant"] = app.merchant
            evidence["app_amount"] = str(app.amount)
            evidence["app_currency"] = app.currency
            evidence["app_txn_date"] = app.transaction_date.isoformat()

        return evidence

    # ------------------------------------------------------------------
    # Fingerprint for idempotency checks
    # ------------------------------------------------------------------

    @staticmethod
    def _build_fingerprint_instruction(
        instruction: ApplyInstruction,
        payload: dict[str, Any],
    ) -> str:
        """Build a deterministic SHA-256 fingerprint of the instruction+payload.

        Used to detect conflicting repeat applications where the same
        decision_id is re-applied with different data.
        """
        canonical = json.dumps(
            {
                "decision_id": instruction.decision_id,
                "queue_item_id": instruction.queue_item_id,
                "action": instruction.action.value,
                "note": instruction.note,
                "reviewer": instruction.reviewer,
                "candidate_id": instruction.candidate_id,
                "payload": payload,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Reference extractors
    # ------------------------------------------------------------------

    @staticmethod
    def _statement_ref(item: ReviewQueueItem) -> str | None:
        stmt = item.candidate.statement
        if stmt is None:
            return None
        return stmt.statement_row_reference or stmt.merchant_raw

    @staticmethod
    def _app_ref(item: ReviewQueueItem) -> str | None:
        app = item.candidate.best_app_transaction
        if app is None:
            return None
        return app.app_txn_id

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def is_applied(self, queue_item_id: str) -> bool:
        """Return True if the given queue item has been resolved."""
        return queue_item_id in self._item_to_decision

    def get_applied_decision_id(self, queue_item_id: str) -> str | None:
        """Return the decision_id applied to the given queue item, or None."""
        return self._item_to_decision.get(queue_item_id)

    def reset(self) -> None:
        """Clear the internal applied-decision registry.

        Useful for testing or re-running resolution workflows.
        """
        self._applied.clear()
        self._item_to_decision.clear()


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------


def _resolve_evidence_refs(
    queue_item_id: str,
    evidence_refs_by_queue_item: Mapping[str, tuple[str, ...]] | None,
) -> tuple[str, ...]:
    """Resolve evidence refs for a queue item from an optional mapping."""
    if evidence_refs_by_queue_item is None:
        return ()
    return evidence_refs_by_queue_item.get(queue_item_id, ())


def apply_decisions(
    items: list[ReviewQueueItem],
    decisions: list[ResolutionDecision],
    *,
    evidence_refs_by_queue_item: Mapping[str, tuple[str, ...]] | None = None,
    require_evidence_ref: bool = False,
) -> list[ResolutionApplyResult]:
    """Apply a list of decisions to matching queue items.

    Validates the batch up-front:
    - Rejects decisions whose ``queue_item_id`` is not in the items list.
    - Rejects duplicate decisions for the same ``queue_item_id``.

    Each call creates a fresh runtime so idempotency and conflict rules
    apply per batch.
    """
    validate_apply_decisions_batch(items, decisions)

    decision_map: dict[str, ResolutionDecision] = {d.queue_item_id: d for d in decisions}
    runtime = ResolutionApplyRuntime()
    results: list[ResolutionApplyResult] = []
    for item in items:
        decision = decision_map.get(item.queue_item_id)
        if decision is None:
            continue
        evidence_refs = _resolve_evidence_refs(item.queue_item_id, evidence_refs_by_queue_item)
        # Build instruction (enforces review→apply boundary)
        try:
            instruction = build_apply_instruction(
                item,
                decision,
                evidence_refs=evidence_refs,
                require_evidence_ref=require_evidence_ref,
            )
            result = runtime.apply_instruction(instruction)
        except ApplyInstructionError as e:
            now_iso = datetime.now(timezone.utc).isoformat()
            result = ResolutionApplyResult(
                apply_id=f"apply-{decision.decision_id}",
                decision_id=decision.decision_id,
                queue_item_id=item.queue_item_id,
                candidate_id=item.candidate.candidate_id if item.candidate.candidate_id else "",
                action=decision.action,
                success=False,
                error_message=str(e),
                audit_evidence={
                    "validation_error": str(e),
                    "action": decision.action.value,
                    "issue_type": item.issue_type.value,
                    "queue_item_id": item.queue_item_id,
                    "decision_id": decision.decision_id,
                    "candidate_id": item.candidate.candidate_id,
                    "evidence_refs": list(evidence_refs),
                    "require_evidence_ref": require_evidence_ref,
                },
                applied_at=now_iso,
                reviewer=decision.reviewer,
                note=decision.note,
            )
        results.append(result)
    return results


def validate_apply_decisions_batch(
    items: list[ReviewQueueItem],
    decisions: list[ResolutionDecision],
) -> None:
    """Validate a batch of decisions before applying them.

    Raises ``ApplyConflictError`` when:

    - A decision's ``queue_item_id`` does not match any item in the batch.
    - Two or more decisions target the same ``queue_item_id``.
    """
    item_ids = {i.queue_item_id for i in items}

    for d in decisions:
        if d.queue_item_id not in item_ids:
            raise ApplyConflictError(
                f"Decision '{d.decision_id}' targets queue_item_id "
                f"'{d.queue_item_id}' which is not present in the items list. "
                f"Every decision must reference a valid item."
            )

    seen: dict[str, str] = {}
    for d in decisions:
        existing_decision_id = seen.get(d.queue_item_id)
        if existing_decision_id is not None:
            raise ApplyConflictError(
                f"Duplicate decisions for queue_item_id '{d.queue_item_id}': "
                f"decisions '{existing_decision_id}' and '{d.decision_id}' "
                f"both target the same queue item."
            )
        seen[d.queue_item_id] = d.decision_id


# ---------------------------------------------------------------------------
# Batch Apply State Control v1
# ---------------------------------------------------------------------------


class BatchApplyStateManager:
    """Stateful manager that enforces batch apply state control.

     Maintains an in-memory registry of batch states to prevent
     duplicate/re-entrant batch application.  This sits on top of the
     existing ResolutionApplyRuntime and apply_decisions
     functions, adding a batch-level state lifecycle.

    State machine::

        PENDING -> APPLYING -> APPLIED  (terminal)
        PENDING -> APPLYING -> FAILED   (retryable)
         APPLIED + duplicate attempt → REJECTED attempt result;
         stored batch state remains APPLIED (terminal, no mutation)
        FAILED  -> APPLYING -> APPLIED | FAILED

     Usage::

         mgr = BatchApplyStateManager()
         result = mgr.apply_batch(
             batch_id="batch-2024-12-01",
             items=review_items,
             decisions=resolution_decisions,
         )
         if result.success:
             print(f"Batch {result.batch_id} applied: {result.applied_count} items")
         elif result.state == BatchApplyState.REJECTED:
             print(f"Batch already applied: {result.error_message}")
    """

    def __init__(self) -> None:
        self._batches: dict[str, BatchApplyState] = {}
        self._batch_results: dict[str, BatchApplyResult] = {}

    # ------------------------------------------------------------------
    # Main apply entry point
    # ------------------------------------------------------------------

    def apply_batch(
        self,
        batch_id: str,
        items: list[ReviewQueueItem],
        decisions: list[ResolutionDecision],
        *,
        evidence_refs_by_queue_item: Mapping[str, tuple[str, ...]] | None = None,
        require_evidence_ref: bool = False,
    ) -> BatchApplyResult:
        """Apply a batch of decisions with state control.

        Enforces the batch state lifecycle:
        1. If the batch is already APPLIED, return REJECTED
           with idempotent=True -- no duplicate effects.
        2. If the batch is in an unsafe state (e.g. APPLYING),
           reject with an error.
        3. Transition to APPLYING, run apply_decisions,
           then transition to APPLIED (all succeeded) or
          FAILED (any failed).

        Returns a BatchApplyResult with the final state, individual
        results, and audit metadata documenting the state transition.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # -- State check: reject duplicate apply of an already-applied batch --
        previous_state = self._batches.get(batch_id)
        if previous_state is not None:
            if previous_state == BatchApplyState.APPLIED:
                return self._build_rejected_result(
                    batch_id=batch_id,
                    previous_state=previous_state,
                    reason="duplicate_apply_blocked",
                    message=(
                        f"Batch '{batch_id}' has already been applied and is in terminal state. "
                        f"Re-application is blocked to prevent duplicate effects."
                    ),
                    timestamp=now_iso,
                )
            if previous_state == BatchApplyState.REJECTED:
                return self._build_rejected_result(
                    batch_id=batch_id,
                    previous_state=previous_state,
                    reason="duplicate_apply_blocked",
                    message=(
                        f"Batch '{batch_id}' was previously rejected and cannot be re-applied."
                    ),
                    timestamp=now_iso,
                )
            if previous_state == BatchApplyState.APPLYING:
                return self._build_rejected_result(
                    batch_id=batch_id,
                    previous_state=previous_state,
                    reason="unsafe_state",
                    message=(
                        f"Batch '{batch_id}' is currently in APPLYING state. Cannot re-enter apply."
                    ),
                    timestamp=now_iso,
                )
            # FAILED or PENDING: allow retry

        # -- Transition to APPLYING --
        self._batches[batch_id] = BatchApplyState.APPLYING

        # -- Run the apply --
        try:
            results = apply_decisions(
                items,
                decisions,
                evidence_refs_by_queue_item=evidence_refs_by_queue_item,
                require_evidence_ref=require_evidence_ref,
            )
        except ApplyConflictError as exc:
            self._batches[batch_id] = BatchApplyState.FAILED
            return self._build_failed_result(
                batch_id=batch_id,
                previous_state=BatchApplyState.APPLYING,
                results=(),
                error_message=str(exc),
                timestamp=now_iso,
            )
        except Exception:
            self._batches[batch_id] = BatchApplyState.FAILED
            raise

        # -- Determine outcome --
        success_count = sum(1 for r in results if r.success)
        fail_count = sum(1 for r in results if not r.success)
        total = len(results)

        if fail_count > 0 or total == 0:
            self._batches[batch_id] = BatchApplyState.FAILED
            return self._build_failed_result(
                batch_id=batch_id,
                previous_state=BatchApplyState.APPLYING,
                results=tuple(results),
                error_message=(
                    f"Batch apply partially or fully failed: {fail_count} of {total} items failed"
                    if total > 0
                    else "Batch apply produced no results"
                ),
                timestamp=now_iso,
            )

        # -- All succeeded: transition to APPLIED (terminal) --
        self._batches[batch_id] = BatchApplyState.APPLIED
        audit = self._build_batch_audit(
            batch_id=batch_id,
            previous_state=BatchApplyState.APPLYING,
            state=BatchApplyState.APPLIED,
            reason="applied_successfully",
            total=total,
            success_count=success_count,
            fail_count=fail_count,
            evidence_refs_by_queue_item=evidence_refs_by_queue_item,
        )
        batch_result = BatchApplyResult(
            batch_id=batch_id,
            state=BatchApplyState.APPLIED,
            results=tuple(results),
            audit_metadata=audit,
            error_message=None,
            batch_applied_at=now_iso,
            idempotent=False,
        )
        self._batch_results[batch_id] = batch_result
        return batch_result

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_batch_state(self, batch_id: str) -> BatchApplyState | None:
        """Return the current state of a batch, or None if unknown."""
        return self._batches.get(batch_id)

    def is_terminal(self, batch_id: str) -> bool:
        """Return True if the batch is in a terminal state."""
        state = self._batches.get(batch_id)
        return state is not None and state.is_terminal

    def get_batch_result(self, batch_id: str) -> BatchApplyResult | None:
        """Return the stored batch result for a terminal batch, or None."""
        return self._batch_results.get(batch_id)

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear the batch state and result registries.

        Useful for testing or re-running batch apply workflows.
        """
        self._batches.clear()
        self._batch_results.clear()

    # ------------------------------------------------------------------
    # Internal builders
    # ------------------------------------------------------------------

    def _build_rejected_result(
        self,
        batch_id: str,
        previous_state: BatchApplyState,
        reason: str,
        message: str,
        timestamp: str,
    ) -> BatchApplyResult:
        """Build a REJECTED batch apply result for a blocked duplicate attempt.

        The stored batch state is not mutated — it remains at its previous
        terminal state (e.g. APPLIED).  Only the returned result signals
        REJECTED so the caller can distinguish a duplicate attempt from a
        fresh application.
        """
        previous_result = self._batch_results.get(batch_id)
        return BatchApplyResult(
            batch_id=batch_id,
            state=BatchApplyState.REJECTED,
            results=(previous_result.results if previous_result is not None else ()),
            audit_metadata={
                "batch_id": batch_id,
                "state": BatchApplyState.REJECTED.value,
                "previous_state": previous_state.value,
                "reason": reason,
                "message": message,
                "attempted_at": timestamp,
                "total_items": len(previous_result.results if previous_result is not None else ()),
                "successful_items": (
                    previous_result.applied_count if previous_result is not None else 0
                ),
                "failed_items": (
                    previous_result.failed_count if previous_result is not None else 0
                ),
                "evidence_refs_tracked": False,
            },
            error_message=message,
            batch_applied_at=timestamp,
            idempotent=True,
        )

    def _build_failed_result(
        self,
        batch_id: str,
        previous_state: BatchApplyState,
        results: tuple[ResolutionApplyResult, ...],
        error_message: str,
        timestamp: str,
    ) -> BatchApplyResult:
        """Build a FAILED batch apply result."""
        total = len(results)
        success_count = sum(1 for r in results if r.success)
        fail_count = total - success_count
        return BatchApplyResult(
            batch_id=batch_id,
            state=BatchApplyState.FAILED,
            results=results,
            audit_metadata={
                "batch_id": batch_id,
                "state": BatchApplyState.FAILED.value,
                "previous_state": previous_state.value,
                "reason": "apply_failed",
                "message": error_message,
                "attempted_at": timestamp,
                "total_items": total,
                "successful_items": success_count,
                "failed_items": fail_count,
                "evidence_refs_tracked": False,
            },
            error_message=error_message,
            batch_applied_at=timestamp,
            idempotent=False,
        )

    @staticmethod
    def _build_batch_audit(
        batch_id: str,
        previous_state: BatchApplyState,
        state: BatchApplyState,
        reason: str,
        total: int,
        success_count: int,
        fail_count: int,
        evidence_refs_by_queue_item: Mapping[str, tuple[str, ...]] | None,
    ) -> dict:
        """Build deterministic audit metadata for a batch state transition."""
        return {
            "batch_id": batch_id,
            "state": state.value,
            "previous_state": previous_state.value,
            "reason": reason,
            "transition_at": datetime.now(timezone.utc).isoformat(),
            "total_items": total,
            "successful_items": success_count,
            "failed_items": fail_count,
            "evidence_refs_tracked": evidence_refs_by_queue_item is not None,
            "apply_runtime_version": "v1",
            "batch_state_control_version": "v1",
        }


__all__ = [
    "ResolutionApplyRuntime",
    "apply_decisions",
    "build_apply_instruction",
    "validate_apply_decisions_batch",
    "BatchApplyStateManager",
    "ApplyInstruction",
    "ApplyInstructionError",
]
