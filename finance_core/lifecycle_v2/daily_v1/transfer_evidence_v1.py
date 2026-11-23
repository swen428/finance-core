"""Verify exact delivered opening questions and independently authored transfer replies."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..contracts import LifecycleError, RuntimeBinding, VerifiedSource, canonical, digest
from .contracts import BundleFactRefV1, exact, instruction_ref, strict_json, text
from .transfer_facts_v1 import project_transfer_fact_v1

QUESTION_FORMAT = "finance-daily-transfer-opening-question-v1"
CHANGE_TARGET_FORMAT = "finance-daily-transfer-change-target-v1"
_CHOICES = {
    "included": "included",
    "excluded": "excluded",
    "已包含": "included",
    "未包含": "excluded",
}


@dataclass(frozen=True)
class VerifiedTransferOpeningReplyV1:
    """The installed evidence port reconstructs this from the original delivered question."""

    reply_evidence_id: str
    question_json: str


@dataclass(frozen=True)
class VerifiedTransferChangeTargetV1:
    """Authenticated association from a human instruction to one retained transfer version."""

    evidence_id: str
    target_json: str


def transfer_change_target_v1(service, conn, source, binding, target, values):
    """Read the original target association without giving the port an expected answer."""
    original = values["instruction_ref"]
    reason = values["reason_ref"]
    link = service.port.transfer_change_target_v1(conn, source["evidence_id"])
    if type(link) is not VerifiedTransferChangeTargetV1:
        raise LifecycleError("Exact independently retained transfer target required")
    text(link.evidence_id, "transfer change association")
    raw = strict_json(link.target_json)
    start, end = reason["span"]
    if (
        link.target_json != canonical(raw)
        or raw
        != {
            "format": CHANGE_TARGET_FORMAT,
            "instruction_ref": original,
            "target_fact_ref": asdict(BundleFactRefV1(**target)),
            "transition": values["transition"],
            "reason_span": reason["span"],
        }
        or original["principal_id"] != binding.principal_id
        or original["client_id"] != binding.client_id
        or source["evidence_id"] != original["evidence_id"]
        or source["raw_input"][start:end] != reason["quote"]
    ):
        raise LifecycleError("Transfer change differs from the retained target or reason")
    return asdict(link)


def _transfer_terms_digest(value: dict[str, Any]) -> str:
    def terms(leg):
        return None if leg is None else {key: val for key, val in leg.items() if key != "coverage"}

    material = {
        key: val for key, val in value.items() if key not in {"outgoing", "incoming", "fee"}
    }
    material.update(
        outgoing=terms(value["outgoing"]),
        incoming=terms(value["incoming"]),
        fee={"mode": value["fee"]["mode"], "charge": terms(value["fee"]["charge"])},
    )
    return digest(material)


def _require_reply(
    binding: RuntimeBinding,
    transfer_sha256: str,
    coverage: dict[str, Any],
    response: VerifiedSource,
    delivered: VerifiedTransferOpeningReplyV1,
) -> dict[str, Any]:
    if (
        type(response) is not VerifiedSource
        or type(delivered) is not VerifiedTransferOpeningReplyV1
    ):
        raise LifecycleError("Exact verified transfer opening reply required")
    if (
        type(delivered.question_json) is not str
        or len(delivered.question_json) > 1024
        or type(delivered.reply_evidence_id) is not str
    ):
        raise LifecycleError("Exact bounded transfer question evidence required")
    retained = strict_json(delivered.question_json)
    exact(
        retained,
        {"format", "delivery_id", "delivered_at", "transfer_sha256", "role_binding_sha256"},
        "delivered transfer opening question",
    )
    original = coverage["binding"]["instruction_ref"]
    reply = instruction_ref(asdict(response), binding)
    delivery_id = text(retained["delivery_id"], "original transfer question delivery")
    if (
        retained["format"] != QUESTION_FORMAT
        or delivered.question_json != canonical(retained)
        or delivered.reply_evidence_id != response.evidence_id
        or reply != coverage["reply_ref"]
        or delivery_id in {original["evidence_id"], reply["evidence_id"]}
        or type(retained["delivered_at"]) is not int
        or not original["source_timestamp"] <= retained["delivered_at"] <= reply["source_timestamp"]
        or retained["transfer_sha256"] != transfer_sha256
        or retained["role_binding_sha256"] != coverage["binding_sha256"]
        or _CHOICES.get(response.raw_input.strip().casefold()) != coverage["treatment"]
    ):
        raise LifecycleError("Transfer opening reply differs from its exact delivered question")
    return retained


def require_transfer_opening_replies_v1(service, conn, value, *, binding=None):
    """Replay actual delivered questions by reply identity inside the owner's read boundary."""
    projection = project_transfer_fact_v1(value)
    if value["status"] != "ACTIVE":
        raise LifecycleError("A reversal must retain its already verified predecessor coverage")
    port = service.port
    binding = service.binding if binding is None else binding
    terms_sha256 = _transfer_terms_digest(value)
    evidence = []
    for effect in projection.effects:
        coverage = effect["coverage"]
        if coverage["basis"] == "authenticated_reply":
            identity = coverage["reply_ref"]["evidence_id"]
            question = _require_reply(
                binding,
                terms_sha256,
                coverage,
                port.source(conn, identity),
                port.transfer_opening_reply_v1(conn, identity),
            )
            evidence.append(
                {"role": effect["role"], "reply_evidence_id": identity, "question": question}
            )
    delivery_ids = {entry["question"]["delivery_id"] for entry in evidence}
    if len(delivery_ids) != len(evidence) or any(
        entry["reply_evidence_id"] in delivery_ids for entry in evidence
    ):
        raise LifecycleError("Transfer roles require distinct original question deliveries")
    return evidence
