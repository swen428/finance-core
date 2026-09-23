"""Shared integrity checks for a successful reconciliation claim bound to 051.

The immutable queue snapshot is the authority for source fields.  A caller's
result, persisted audit, and action payload may only describe that source.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping

from finance_core.reconciliation.models import (
    ResolutionAction,
    ResolutionApplyResult,
    ResolutionResult,
    validate_resolution_decision,
)

if TYPE_CHECKING:
    from finance_core.reconciliation.source_binding import BoundQueue

_SOURCE_AUDIT_KEYS = frozenset(
    {
        "queue_item_id",
        "candidate_id",
        "issue_type",
        "confidence_score",
        "reason_codes",
        "evidence_summary",
        "statement_merchant",
        "statement_amount",
        "statement_currency",
        "statement_txn_date",
        "statement_posted_date",
        "app_txn_id",
        "app_merchant",
        "app_amount",
        "app_currency",
        "app_txn_date",
    }
)
_CLASSIFICATION = frozenset({ResolutionAction.CONFIRM_MATCH, ResolutionAction.MARK_DUPLICATE})
_RESOLUTION_ENVELOPE_KEYS = frozenset({"resolution_action", "resolved_at", "reviewer", "note"})
_RESOLUTION_PERSISTED_KEYS = frozenset(
    {"canonical_app_transaction_ref", "payload_target_app_txn_id"}
)
_RESOLUTION_DUPLICATE_KEYS = frozenset({"duplicate_app_txn_ids", "kept_app_txn_id", "audit_only"})
_APPLY_ENVELOPE_KEYS = frozenset(
    {
        "apply_runtime_version",
        "instruction_id",
        "resolution_action",
        "reviewer",
        "note",
        "applied_timestamp",
        "decision_id",
        "evidence_refs",
    }
)


def require_nonempty(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"successful reconciliation {label} must be a nonempty string")
    return value


def require_note(value: object) -> str:
    if type(value) is not str:
        raise ValueError("successful reconciliation note must be a string")
    return value


def require_aware_time(value: object, label: str) -> str:
    require_nonempty(value, label)
    try:
        parsed = datetime.fromisoformat(value)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ValueError(f"successful reconciliation {label} is not ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"successful reconciliation {label} must be timezone aware")
    return value  # type: ignore[return-value]


def assert_source_audit(evidence: Mapping[str, Any], bound: BoundQueue) -> None:
    """Require every producer source field, including optional-field absence."""
    from finance_core.reconciliation.source_binding import bound_source_audit_projection

    expected = bound_source_audit_projection(bound)
    if {key: evidence[key] for key in _SOURCE_AUDIT_KEYS if key in evidence} != expected:
        raise ValueError("successful reconciliation audit differs from bound queue source")


def assert_resolution_audit_schema(
    evidence: Mapping[str, Any], bound: BoundQueue, action: ResolutionAction, *, persisted: bool
) -> None:
    """Verify exact action/stage keys; provenance cannot be added by a caller."""
    from finance_core.reconciliation.source_binding import bound_source_audit_projection

    expected = set(bound_source_audit_projection(bound)) | _RESOLUTION_ENVELOPE_KEYS
    if persisted:
        expected.update(_RESOLUTION_PERSISTED_KEYS)
        if action == ResolutionAction.MARK_DUPLICATE:
            expected.update(_RESOLUTION_DUPLICATE_KEYS)
    if set(evidence) != expected:
        raise ValueError("successful reconciliation audit has unexpected or missing fields")
    assert_source_audit(evidence, bound)
    if persisted:
        canonical = bound.app_transaction_ref
        if (
            evidence.get("canonical_app_transaction_ref") != canonical
            or evidence.get("payload_target_app_txn_id") != canonical
        ):
            raise ValueError("successful reconciliation stored target differs from frozen queue")
        if action == ResolutionAction.MARK_DUPLICATE:
            if (
                evidence.get("duplicate_app_txn_ids")
                != [app.app_txn_id for app in bound.app_transactions]
                or evidence.get("kept_app_txn_id") != canonical
                or evidence.get("audit_only") is not True
            ):
                raise ValueError("successful duplicate audit differs from frozen queue")


def assert_apply_audit_schema(
    evidence: Mapping[str, Any], bound: BoundQueue, decision_id: str
) -> None:
    """Verify the complete apply audit shape and derived producer metadata."""
    from finance_core.reconciliation.source_binding import bound_source_audit_projection

    expected = set(bound_source_audit_projection(bound)) | _APPLY_ENVELOPE_KEYS
    if set(evidence) != expected:
        raise ValueError("successful reconciliation apply audit has unexpected or missing fields")
    assert_source_audit(evidence, bound)
    # These refs are caller-supplied auxiliary traceability only.  They are
    # not bound source members, verified targets, or authorization evidence.
    refs = evidence.get("evidence_refs")
    if (
        evidence.get("apply_runtime_version") != "v1"
        or evidence.get("instruction_id") != f"instr-{decision_id}"
        or type(refs) is not list
        or any(type(ref) is not str or not ref.strip() for ref in refs)
    ):
        raise ValueError("successful reconciliation apply audit producer metadata is invalid")


def assert_resolution_envelope(result: ResolutionResult, bound: BoundQueue) -> None:
    decision = result.decision
    evidence = result.audit_evidence
    assert_resolution_basic(result)
    if (
        type(result.success) is not bool
        or result.success is not True
        or type(evidence) is not dict
        or decision.action not in _CLASSIFICATION
        or not validate_resolution_decision(decision.action, bound.issue_type)[0]
    ):
        raise ValueError("successful reconciliation classification is invalid")
    if (
        decision.queue_item_id != bound.queue_item_id
        or result.queue_item.queue_item_id != bound.queue_item_id
        or evidence.get("resolution_action") != decision.action.value
        or evidence.get("reviewer") != decision.reviewer
        or evidence.get("note") != decision.note
        or (
            decision.resolved_at is not None and evidence.get("resolved_at") != decision.resolved_at
        )
    ):
        raise ValueError("successful reconciliation evidence source identity changed")
    assert_resolution_audit_schema(evidence, bound, decision.action, persisted=False)


def assert_apply_envelope(result: ResolutionApplyResult, bound: BoundQueue) -> None:
    evidence = result.audit_evidence
    assert_apply_basic(result)
    if (
        type(result.success) is not bool
        or result.success is not True
        or type(result.idempotent) is not bool
        or type(evidence) is not dict
        or type(result.payload) is not dict
        or result.action not in _CLASSIFICATION
        or not validate_resolution_decision(result.action, bound.issue_type)[0]
    ):
        raise ValueError("successful reconciliation apply action conflicts with frozen queue issue")
    if (
        result.queue_item_id != bound.queue_item_id
        or result.candidate_id != bound.candidate_id
        or evidence.get("resolution_action") != result.action.value
        or evidence.get("decision_id") != result.decision_id
        or evidence.get("reviewer") != result.reviewer
        or evidence.get("note") != result.note
        or result.statement_reference != bound.statement_transaction_ref
        or result.app_transaction_reference != bound.app_transaction_ref
    ):
        raise ValueError(
            "successful reconciliation apply evidence differs from frozen queue source"
        )
    assert_apply_audit_schema(evidence, bound, result.decision_id)
    assert_apply_payload(result.payload, result.action, bound)


def assert_apply_payload(
    payload: Mapping[str, Any], action: ResolutionAction, bound: BoundQueue
) -> None:
    """Compare the complete producer payload, including fixed operation text."""
    from finance_core.reconciliation.apply import ResolutionApplyRuntime

    candidate = bound.item.candidate
    if action == ResolutionAction.CONFIRM_MATCH:
        expected = ResolutionApplyRuntime._payload_confirm_match(candidate)
    elif action == ResolutionAction.MARK_DUPLICATE:
        expected = ResolutionApplyRuntime._payload_mark_duplicate(candidate)
    else:
        raise ValueError("unsupported bound apply classification")
    if payload != expected:
        raise ValueError("successful reconciliation payload differs from frozen queue source")


def assert_resolution_row(
    row: Mapping[str, Any],
    decision: Mapping[str, Any],
    evidence: Mapping[str, Any],
    bound: BoundQueue,
) -> None:
    assert_resolution_row_basic(row, decision, evidence)
    if (
        row["review_queue_public_id"] != bound.queue_item_id
        or decision.get("review_queue_public_id") != bound.queue_item_id
    ):
        raise ValueError("successful reconciliation stored decision queue differs")
    try:
        action = ResolutionAction(decision["decision_action"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("successful reconciliation stored action is invalid") from exc
    if (
        action not in _CLASSIFICATION
        or not validate_resolution_decision(action, bound.issue_type)[0]
    ):
        raise ValueError("successful reconciliation stored action conflicts with queue")
    assert_resolution_audit_schema(evidence, bound, action, persisted=True)


def assert_resolution_basic(result: ResolutionResult) -> None:
    """Check success metadata regardless of the decision action under 051."""
    decision = result.decision
    evidence = result.audit_evidence
    if (
        type(result.success) is not bool
        or result.success is not True
        or result.error_message is not None
        or type(evidence) is not dict
    ):
        raise ValueError("successful reconciliation result envelope is invalid")
    require_nonempty(result.result_id, "result_id")
    require_nonempty(decision.decision_id, "decision_id")
    require_nonempty(decision.queue_item_id, "queue_item_id")
    require_nonempty(result.queue_item.queue_item_id, "queue_item_id")
    require_nonempty(decision.reviewer, "reviewer")
    require_note(decision.note)
    if decision.resolved_at is not None:
        require_aware_time(decision.resolved_at, "resolved_at")
    require_aware_time(evidence.get("resolved_at"), "audit resolved_at")
    if (
        decision.queue_item_id != result.queue_item.queue_item_id
        or evidence.get("queue_item_id") != decision.queue_item_id
        or evidence.get("candidate_id") != result.queue_item.candidate.candidate_id
        or evidence.get("resolution_action") != decision.action.value
        or evidence.get("issue_type") != result.queue_item.issue_type.value
        or evidence.get("reviewer") != decision.reviewer
        or evidence.get("note") != decision.note
        or (
            decision.resolved_at is not None and evidence.get("resolved_at") != decision.resolved_at
        )
    ):
        raise ValueError("successful reconciliation evidence source identity changed")


def assert_apply_basic(result: ResolutionApplyResult) -> None:
    evidence = result.audit_evidence
    if (
        type(result.success) is not bool
        or result.success is not True
        or type(result.idempotent) is not bool
        or result.error_message is not None
        or type(evidence) is not dict
        or type(result.payload) is not dict
    ):
        raise ValueError("successful reconciliation apply envelope is invalid")
    for label, value in (
        ("apply_id", result.apply_id),
        ("decision_id", result.decision_id),
        ("queue_item_id", result.queue_item_id),
        ("candidate_id", result.candidate_id),
        ("reviewer", result.reviewer),
    ):
        require_nonempty(value, label)
    require_note(result.note)
    require_aware_time(result.applied_at, "applied_at")
    require_aware_time(evidence.get("applied_timestamp"), "audit applied_timestamp")
    if (
        evidence.get("queue_item_id") != result.queue_item_id
        or evidence.get("candidate_id") != result.candidate_id
        or evidence.get("decision_id") != result.decision_id
        or evidence.get("resolution_action") != result.action.value
        or evidence.get("reviewer") != result.reviewer
        or evidence.get("note") != result.note
    ):
        raise ValueError("successful reconciliation apply source identity changed")


def assert_resolution_row_basic(
    row: Mapping[str, Any], decision: Mapping[str, Any], evidence: Mapping[str, Any]
) -> None:
    require_nonempty(row.get("result_id"), "result_id")
    require_nonempty(row.get("decision_public_id"), "decision_id")
    require_nonempty(row.get("review_queue_public_id"), "queue_item_id")
    if row.get("success") != 1 or type(row.get("success")) is not int:
        raise ValueError("successful reconciliation stored success flag is invalid")
    if row.get("error_message") is not None:
        raise ValueError("successful reconciliation stored success has error")
    require_nonempty(decision.get("reviewer"), "reviewer")
    note = decision.get("decision_note")
    if note is None:
        note = ""
    require_note(note)
    resolved_at = decision.get("resolved_at")
    if resolved_at is not None:
        require_aware_time(resolved_at, "resolved_at")
    require_aware_time(evidence.get("resolved_at"), "audit resolved_at")
    if (
        row["decision_public_id"] != decision.get("public_id")
        or decision.get("review_queue_public_id") != row["review_queue_public_id"]
        or evidence.get("queue_item_id") != row["review_queue_public_id"]
        or evidence.get("resolution_action") != decision.get("decision_action")
        or evidence.get("reviewer") != decision.get("reviewer")
        or evidence.get("note") != note
        or (resolved_at is not None and evidence.get("resolved_at") != resolved_at)
    ):
        raise ValueError("successful reconciliation stored decision audit differs")


def assert_apply_row(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    evidence: Mapping[str, Any],
    statement_ref: str | None,
    app_ref: str | None,
    bound: BoundQueue,
) -> None:
    assert_apply_row_basic(row, evidence)
    if (
        row["queue_item_id"] != bound.queue_item_id
        or row["candidate_id"] != bound.candidate_id
        or statement_ref != bound.statement_transaction_ref
        or app_ref != bound.app_transaction_ref
    ):
        raise ValueError("successful reconciliation stored apply source differs")
    try:
        action = ResolutionAction(row["action"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("successful reconciliation stored apply action is invalid") from exc
    if (
        action not in _CLASSIFICATION
        or not validate_resolution_decision(action, bound.issue_type)[0]
    ):
        raise ValueError("successful reconciliation stored apply action conflicts with queue")
    assert_apply_audit_schema(evidence, bound, row["decision_id"])
    assert_apply_payload(payload, action, bound)


def assert_apply_row_basic(row: Mapping[str, Any], evidence: Mapping[str, Any]) -> None:
    for label in ("apply_id", "decision_id", "queue_item_id", "candidate_id", "reviewer"):
        require_nonempty(row.get(label), label)
    note = row.get("note")
    if note is None:
        note = ""
    require_note(note)
    require_aware_time(row.get("applied_at"), "applied_at")
    require_aware_time(evidence.get("applied_timestamp"), "audit applied_timestamp")
    if row.get("success") != 1 or type(row.get("success")) is not int:
        raise ValueError("successful reconciliation stored success flag is invalid")
    if row.get("error_message") is not None:
        raise ValueError("successful reconciliation stored success has error")
    if row.get("idempotent") not in (0, 1) or type(row.get("idempotent")) is not int:
        raise ValueError("successful reconciliation stored idempotent flag is invalid")
    if (
        evidence.get("queue_item_id") != row["queue_item_id"]
        or evidence.get("candidate_id") != row["candidate_id"]
        or evidence.get("decision_id") != row["decision_id"]
        or evidence.get("resolution_action") != row.get("action")
        or evidence.get("reviewer") != row["reviewer"]
        or evidence.get("note") != note
    ):
        raise ValueError("successful reconciliation stored apply audit differs")
