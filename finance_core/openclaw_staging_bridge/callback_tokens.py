"""Stateless callback-token contract for bridge decision commands.

Implements architecture §7.4: one HMAC-SHA256 token per action, binding the
proposal public ID, exact current version, effective content hash, action,
and plaintext expiry.  No token state is persisted; tampering with any bound
value invalidates the token.  Key material never leaves this module except
as in-memory HMAC input.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

TOKEN_PREFIX = "fcb_v1_"
_TOKEN_BODY_LENGTH = 32
# Exactly the urlsafe-base64 alphabet: legal token bodies are produced by
# ``base64.urlsafe_b64encode``.  Restricting the well-formedness check to
# this alphabet keeps tampered or out-of-alphabet tokens on the stable
# CALLBACK_TOKEN_INVALID refusal path instead of an encoding failure.
_TOKEN_BODY_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

ACTION_CONFIRM = "confirm"
ACTION_EDIT = "edit"
ACTION_REJECT = "reject"

DECISION_ACTIONS = (ACTION_CONFIRM, ACTION_EDIT, ACTION_REJECT)


def _message(
    *,
    proposal_public_id: str,
    version: int,
    content_hash: str,
    action: str,
    expiry: int,
) -> bytes:
    return "|".join([proposal_public_id, str(version), content_hash, action, str(expiry)]).encode(
        "utf-8"
    )


def compute_token(
    key: bytes,
    *,
    proposal_public_id: str,
    version: int,
    content_hash: str,
    action: str,
    expiry: int,
) -> str:
    digest = hmac.new(
        key,
        _message(
            proposal_public_id=proposal_public_id,
            version=version,
            content_hash=content_hash,
            action=action,
            expiry=expiry,
        ),
        hashlib.sha256,
    ).digest()
    body = base64.urlsafe_b64encode(digest).decode("ascii")[:_TOKEN_BODY_LENGTH]
    return f"{TOKEN_PREFIX}{body}"


def issue_callback_tokens(
    key: bytes,
    *,
    proposal_public_id: str,
    version: int,
    content_hash: str,
    expiry: int,
) -> dict[str, dict[str, object]]:
    """Issue one bounded token entry per decision action."""
    return {
        action: {
            "token": compute_token(
                key,
                proposal_public_id=proposal_public_id,
                version=version,
                content_hash=content_hash,
                action=action,
                expiry=expiry,
            ),
            "expiry": expiry,
        }
        for action in DECISION_ACTIONS
    }


def token_body_is_well_formed(token: object) -> bool:
    return (
        isinstance(token, str)
        and token.startswith(TOKEN_PREFIX)
        and len(token) == len(TOKEN_PREFIX) + _TOKEN_BODY_LENGTH
        and set(token[len(TOKEN_PREFIX) :]) <= _TOKEN_BODY_ALPHABET
    )


def verify_token(
    key: bytes,
    *,
    token: str,
    proposal_public_id: str,
    version: int,
    content_hash: str,
    action: str,
    expiry: int,
) -> bool:
    """Constant-time verification against the durable bound values."""
    if not token_body_is_well_formed(token):
        return False
    expected = compute_token(
        key,
        proposal_public_id=proposal_public_id,
        version=version,
        content_hash=content_hash,
        action=action,
        expiry=expiry,
    )
    return hmac.compare_digest(expected.encode("ascii"), token.encode("ascii"))


def find_mismatched_action(
    key: bytes,
    *,
    token: str,
    proposal_public_id: str,
    version: int,
    content_hash: str,
    expected_action: str,
    expiry: int,
) -> str | None:
    """Return the other action whose token matches, if any (wrong-action detection)."""
    for action in DECISION_ACTIONS:
        if action == expected_action:
            continue
        if verify_token(
            key,
            token=token,
            proposal_public_id=proposal_public_id,
            version=version,
            content_hash=content_hash,
            action=action,
            expiry=expiry,
        ):
            return action
    return None


__all__ = [
    "ACTION_CONFIRM",
    "ACTION_EDIT",
    "ACTION_REJECT",
    "DECISION_ACTIONS",
    "TOKEN_PREFIX",
    "compute_token",
    "find_mismatched_action",
    "issue_callback_tokens",
    "token_body_is_well_formed",
    "verify_token",
]
