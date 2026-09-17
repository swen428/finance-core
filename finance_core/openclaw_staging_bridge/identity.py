"""Deterministic operation and result identities for the bridge CLI.

Every derived identity is a pure function of the request's durable command
material, so identical replays reproduce identical identities (design §5.3).
Result identities owned by Finance services (intake/proposal public IDs
produced elsewhere) are echoed unchanged.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_digest(*parts: str) -> str:
    """SHA-256 hex digest over canonical NUL-separated identity parts."""
    material = "\x00".join(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def operation_id(command: str, idempotency_key: str | None, arguments: dict[str, Any]) -> str:
    canonical_arguments = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    digest = canonical_digest("operation", command, idempotency_key or "", canonical_arguments)
    return f"op_{digest[:32]}"


def capture_identities(idempotency_key: str) -> dict[str, str]:
    """Deterministic caller-owned identities for one receipt capture command."""
    digest = canonical_digest("openclaw-bridge-capture-v1", idempotency_key)
    return {
        "raw_intake_public_id": f"raw_intake_bridge_{digest[:32]}",
        "attachment_evidence_public_id": f"tgae_bridge_{digest[:32]}",
        "extraction_public_id": f"rocr_bridge_{digest[:32]}",
        "proposal_public_id": f"prop_bridge_{digest[:32]}",
        "link_public_id": f"ropl_bridge_{digest[:32]}",
    }


def confirmation_public_id(idempotency_key: str) -> str:
    digest = canonical_digest("openclaw-bridge-confirm-v1", idempotency_key)
    return f"pca_bridge_{digest[:32]}"


def completion_public_id(idempotency_key: str) -> str:
    digest = canonical_digest("openclaw-bridge-edit-completion-v1", idempotency_key)
    return f"pco_bridge_{digest[:32]}"


def correction_public_id(idempotency_key: str) -> str:
    digest = canonical_digest("openclaw-bridge-edit-correction-v1", idempotency_key)
    return f"rcor_bridge_{digest[:32]}"


def receipt_conversion_command_public_id(proposal_public_id: str, content_hash: str) -> str:
    """Deterministic caller-owned receipt conversion command identity (S4).

    Bound to the proposal identity and the durable effective content hash
    verified inside ``finalize``, so identical replays reproduce the same
    command identity and the conversion boundary's ``rpfc_`` ID contract is
    satisfied without any envelope-carried command material.
    """
    digest = canonical_digest(
        "openclaw-bridge-finalize-conversion-v1", proposal_public_id, content_hash
    )
    return f"rpfc_bridge_{digest[:32]}"


def ai_fallback_idempotency_key(command: str, public_id: str) -> str:
    """Derive the v1 fallback command key from its exact command identity."""
    if command not in {
        "prepare_ai_fallback",
        "claim_ai_fallback_invocation",
        "record_ai_fallback_result",
    }:
        raise ValueError("Unsupported AI fallback command.")
    if not isinstance(public_id, str) or not public_id:
        raise ValueError("AI fallback public ID must be non-empty text.")
    domain = {
        "prepare_ai_fallback": "finance-aifp-key-v1",
        "claim_ai_fallback_invocation": "finance-aifc-key-v1",
        "record_ai_fallback_result": "finance-aifr-key-v1",
    }[command]
    payload = bytearray(domain.encode("ascii"))
    payload.extend(b"\x00")
    payload.extend((2).to_bytes(4, "big"))
    for field in (command, public_id):
        encoded = field.encode("utf-8")
        payload.extend(len(encoded).to_bytes(8, "big"))
        payload.extend(encoded)
    return {
        "prepare_ai_fallback": "aifp_",
        "claim_ai_fallback_invocation": "aifc_",
        "record_ai_fallback_result": "aifr_",
    }[command] + hashlib.sha256(payload).hexdigest()


def ai_fallback_v2_idempotency_key(command: str, public_id: str) -> str:
    """Derive a v2-only key without widening or changing the v1 identity map."""
    domains = {
        "prepare_ai_fallback_v2": ("finance-aifp-key-v2", "aifp2_"),
        "claim_ai_fallback_invocation_v2": ("finance-aifc-key-v2", "aifc2_"),
        "record_ai_fallback_result_v2": ("finance-aifr-key-v2", "aifr2_"),
    }
    if command not in domains:
        raise ValueError("Unsupported AI fallback v2 command.")
    if not isinstance(public_id, str) or not public_id:
        raise ValueError("AI fallback v2 public ID must be non-empty text.")
    domain, prefix = domains[command]
    payload = bytearray(domain.encode("ascii"))
    payload.extend(b"\x00")
    payload.extend((2).to_bytes(4, "big"))
    for field in (command, public_id):
        encoded = field.encode("utf-8")
        payload.extend(len(encoded).to_bytes(8, "big"))
        payload.extend(encoded)
    return prefix + hashlib.sha256(payload).hexdigest()


__all__ = [
    "canonical_digest",
    "ai_fallback_idempotency_key",
    "ai_fallback_v2_idempotency_key",
    "capture_identities",
    "completion_public_id",
    "confirmation_public_id",
    "correction_public_id",
    "operation_id",
    "receipt_conversion_command_public_id",
]
