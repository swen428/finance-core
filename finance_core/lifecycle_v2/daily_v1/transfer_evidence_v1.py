"""Verify exact delivered opening questions and independently authored transfer replies."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..contracts import LifecycleError, RuntimeBinding, VerifiedSource, canonical, digest
from .contracts import exact, instruction_ref, strict_json, text
from .transfer_facts_v1 import project_transfer_fact_v1

QUESTION_FORMAT = "finance-daily-transfer-opening-question-v1"
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
) -> None:
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


def require_transfer_opening_replies_v1(service, conn, value, *, binding=None):
    """Replay actual delivered questions by reply identity inside the owner's read boundary."""
    projection = project_transfer_fact_v1(value)
    if value["status"] != "ACTIVE":
        raise LifecycleError("A reversal must retain its already verified predecessor coverage")
    port = service.port
    binding = service.binding if binding is None else binding
    terms_sha256 = _transfer_terms_digest(value)
    for effect in projection.effects:
        coverage = effect["coverage"]
        if coverage["basis"] == "authenticated_reply":
            identity = coverage["reply_ref"]["evidence_id"]
            _require_reply(
                binding,
                terms_sha256,
                coverage,
                port.source(conn, identity),
                port.transfer_opening_reply_v1(conn, identity),
            )
