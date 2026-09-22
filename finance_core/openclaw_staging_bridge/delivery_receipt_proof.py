"""Authenticated, non-financial proof for one host-consumed delivery receipt."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path

from finance_core.receipt_staging_runner.workspace import load_delivery_receipt_signing_key

PROOF_VERSION = "finance_delivery_receipt_proof_v1"
_CAPABILITY = object()


def _field(tag: str, value: bytes) -> bytes:
    tag_bytes = tag.encode("ascii")
    if not tag_bytes or len(tag_bytes) > 0xFFFF or len(value) > 0xFFFFFFFF:
        raise ValueError("delivery receipt proof field is out of range")
    return len(tag_bytes).to_bytes(2, "big") + tag_bytes + len(value).to_bytes(4, "big") + value


def _proof_material(
    *,
    workspace_path: str,
    attempt_nonce: str,
    capability: str,
    delivery_material_version: str,
    delivery_material_sha256: str,
    provider_message_id: int | str,
    receipt_token_sha256: str,
    channel: str,
    account_id: str,
    conversation_id: str,
    session_key: str,
    source_identity_sha256: str,
) -> bytes:
    values = (
        ("version", PROOF_VERSION),
        ("workspace_path", workspace_path),
        ("attempt_nonce", attempt_nonce),
        ("capability", capability),
        ("delivery_material_version", delivery_material_version),
        ("delivery_material_sha256", delivery_material_sha256),
        ("provider_message_id", str(provider_message_id)),
        ("receipt_token_sha256", receipt_token_sha256),
        ("channel", channel),
        ("account_id", account_id),
        ("conversation_id", conversation_id),
        ("session_key", session_key),
        ("source_identity_sha256", source_identity_sha256),
    )
    try:
        return b"".join(_field(tag, value.encode("utf-8")) for tag, value in values)
    except (AttributeError, UnicodeEncodeError) as exc:
        raise ValueError("delivery receipt proof field is malformed") from exc


def receipt_proof_sha256(*, signing_key: bytes, **fields: object) -> str:
    """Return the domain-separated HMAC used by the exact Bridge consumer."""
    if not isinstance(signing_key, bytes) or len(signing_key) != 32:
        raise ValueError("delivery receipt signing key is malformed")
    material = _proof_material(**fields)  # type: ignore[arg-type]
    return hmac.new(signing_key, material, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class VerifiedDeliveryReceipt:
    workspace_path: str
    attempt_nonce: str
    capability: str
    delivery_material_version: str
    delivery_material_sha256: str
    provider_message_id: int | str
    receipt_token_sha256: str
    channel: str
    account_id: str
    conversation_id: str
    session_key: str
    source_identity_sha256: str
    _capability: object


def authenticate_delivery_receipt(
    *,
    receipt_proof_sha256_value: str,
    **fields: object,
) -> VerifiedDeliveryReceipt:
    """Authenticate all fields with the key owned by the bound workspace."""
    if not isinstance(receipt_proof_sha256_value, str) or len(receipt_proof_sha256_value) != 64:
        raise ValueError("delivery receipt proof is malformed")
    workspace_path = fields.get("workspace_path")
    if not isinstance(workspace_path, str) or not workspace_path:
        raise ValueError("delivery receipt workspace is malformed")
    workspace = Path(workspace_path)
    if (
        not workspace.is_absolute()
        or workspace.is_symlink()
        or not workspace.is_dir()
        or workspace.resolve(strict=True) != workspace
    ):
        raise ValueError("delivery receipt workspace is unsafe")
    signing_key = load_delivery_receipt_signing_key(str(workspace / "runtime"))
    expected = receipt_proof_sha256(signing_key=signing_key, **fields)
    if not hmac.compare_digest(expected, receipt_proof_sha256_value):
        raise ValueError("delivery receipt proof is invalid")
    return VerifiedDeliveryReceipt(**fields, _capability=_CAPABILITY)  # type: ignore[arg-type]


def require_verified_delivery_receipt(value: object) -> VerifiedDeliveryReceipt:
    if not isinstance(value, VerifiedDeliveryReceipt) or value._capability is not _CAPABILITY:
        raise ValueError("host-authenticated delivery receipt is required")
    return value


__all__ = [
    "PROOF_VERSION",
    "VerifiedDeliveryReceipt",
    "authenticate_delivery_receipt",
    "receipt_proof_sha256",
    "require_verified_delivery_receipt",
]
