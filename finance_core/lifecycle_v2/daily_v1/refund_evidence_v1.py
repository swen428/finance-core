"""Reconstruct retained refund selection and opening answers by actual evidence identity."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..contracts import LifecycleError, RuntimeBinding, VerifiedSource, canonical
from .contracts import BundleFactRefV1, exact, instruction_ref, strict_json, text
from .refund_facts_v1 import project_refund_fact_v1

TARGET_FORMAT = "finance-daily-refund-target-v1"
OPENING_QUESTION_FORMAT = "finance-daily-refund-opening-question-v1"
_OPENING_CHOICES = {
    "included": "included",
    "excluded": "excluded",
    "已包含": "included",
    "未包含": "excluded",
}


@dataclass(frozen=True)
class VerifiedRefundTargetV1:
    evidence_id: str
    target_json: str


@dataclass(frozen=True)
class VerifiedRefundOpeningReplyV1:
    reply_evidence_id: str
    question_json: str


def _require_target(
    retained: VerifiedRefundTargetV1,
    source: VerifiedSource,
    value: dict[str, Any],
    previous: BundleFactRefV1 | None,
) -> None:
    if type(retained) is not VerifiedRefundTargetV1:
        raise LifecycleError("Exact independently retained refund selection required")
    text(retained.evidence_id, "refund selection evidence")
    selected = strict_json(retained.target_json)
    reason = value["reason_ref"]
    span = None
    if reason is not None:
        start, end = reason["span"]
        span = [start, end]
        if source.raw_input[start:end] != reason["quote"]:
            raise LifecycleError("Refund reason differs from the original instruction")
    if (
        retained.target_json != canonical(selected)
        or selected
        != {
            "format": TARGET_FORMAT,
            "transition": value["transition"],
            "instruction_ref": value["instruction_ref"],
            "original_expense": value["expense"],
            "refund_predecessor": asdict(previous) if previous is not None else None,
            "reason_span": span,
        }
    ):
        raise LifecycleError("Refund selection differs from the retained expense, target or reason")


def _require_opening_reply(
    binding: RuntimeBinding,
    coverage: dict[str, Any],
    reply: VerifiedSource,
    retained: VerifiedRefundOpeningReplyV1,
) -> dict[str, Any]:
    if type(reply) is not VerifiedSource or type(retained) is not VerifiedRefundOpeningReplyV1:
        raise LifecycleError("Exact verified refund opening reply required")
    actual = instruction_ref(asdict(reply), binding)
    question = strict_json(retained.question_json)
    exact(question, {"format", "delivery_id", "delivered_at", "credit_sha256"}, "refund question")
    original = coverage["binding"]["instruction_ref"]
    delivery = text(question["delivery_id"], "refund opening question delivery")
    if (
        question["format"] != OPENING_QUESTION_FORMAT
        or retained.question_json != canonical(question)
        or retained.reply_evidence_id != reply.evidence_id
        or actual != coverage["reply_ref"]
        or delivery in {original["evidence_id"], reply.evidence_id}
        or type(question["delivered_at"]) is not int
        or not original["source_timestamp"] <= question["delivered_at"] <= reply.source_timestamp
        or question["credit_sha256"] != coverage["binding_sha256"]
        or _OPENING_CHOICES.get(reply.raw_input.strip().casefold()) != coverage["treatment"]
    ):
        raise LifecycleError("Refund opening reply differs from its exact delivered question")
    return question


def verify_refund_evidence_v1(
    service,
    conn,
    binding: RuntimeBinding,
    source: VerifiedSource,
    value: dict[str, Any],
    previous: BundleFactRefV1 | None,
) -> dict[str, Any]:
    """Return canonical verified material for later replay under the original binding."""
    projected = project_refund_fact_v1(value)
    if not conn.in_transaction:
        raise LifecycleError("Refund evidence requires one protected ledger transaction")
    if (
        type(source) is not VerifiedSource
        or instruction_ref(asdict(source), binding) != value["instruction_ref"]
    ):
        raise LifecycleError("Refund proposal differs from its authenticated instruction")
    if (previous is None) != (value["transition"] == "refund_create") or (
        previous is not None and type(previous) is not BundleFactRefV1
    ):
        raise LifecycleError("Exact refund change predecessor required")
    port = service.port
    link = port.refund_target_v1(conn, source.evidence_id)
    _require_target(link, source, value, previous)
    retained = {"selection": asdict(link), "opening_question": None}
    if projected.credit_effect is not None:
        coverage = projected.credit_effect["coverage"]
        if coverage["basis"] == "authenticated_reply":
            identity = coverage["reply_ref"]["evidence_id"]
            question = _require_opening_reply(
                binding,
                coverage,
                port.source(conn, identity),
                port.refund_opening_reply_v1(conn, identity),
            )
            if link.evidence_id in {identity, question["delivery_id"]}:
                raise LifecycleError("Refund selection and opening response require separate proof")
            retained["opening_question"] = {"reply_evidence_id": identity, "question": question}
    return retained
