"""Structural private-DM policy for bridge Telegram capture commands.

The CLI independently re-verifies the Telegram source shape instead of
trusting the upstream plugin: both text and receipt capture only accept
private direct messages.  Group and supergroup chat IDs are negative in the
Bot API and are refused; when a raw update carries ``chat.type`` it must be
``private``; and the sender identity must match the chat identity, because a
private DM's chat ID equals the user's ID in the Bot API identity model.
These structural refusals happen before any persistence.  Deployment-time
operator allowlisting remains a separately authorized OpenClaw configuration
stage; this boundary is the CLI-side structural gate only.
"""

from __future__ import annotations

from typing import Mapping

from finance_core.openclaw_staging_bridge import errors


def _refuse(message: str) -> errors.BridgeError:
    return errors.bridge_error(
        errors.TELEGRAM_SOURCE_REFUSED,
        message,
        errors.EXIT_VALIDATION_REFUSED,
    )


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0:
        return None
    return value


def validate_private_direct_message(payload: object) -> None:
    """Refuse non-private Telegram message structures before persistence."""
    if not isinstance(payload, Mapping):
        raise _refuse("Telegram update payload must be a mapping.")
    message = payload.get("message")
    if not isinstance(message, Mapping):
        # Non-message updates are rejected by the text adapter with its own
        # stable reason; the DM policy only governs message structures.
        return
    chat = message.get("chat")
    if not isinstance(chat, Mapping):
        raise _refuse("Telegram message must carry a chat object.")
    chat_id = _positive_int(chat.get("id"))
    if chat_id is None:
        raise _refuse(
            "Telegram chat ID must be a positive private-DM identifier; "
            "group and supergroup chats are refused."
        )
    chat_type = chat.get("type")
    if chat_type is not None and chat_type != "private":
        raise _refuse("Only private Telegram direct messages are accepted.")
    sender = message.get("from")
    if not isinstance(sender, Mapping):
        raise _refuse("Private Telegram direct messages must identify a sender.")
    sender_id = _positive_int(sender.get("id"))
    if sender_id is None:
        raise _refuse("Telegram sender ID must be a positive integer.")
    if sender_id != chat_id:
        raise _refuse("Telegram sender identity does not match the private chat identity.")


def validate_private_direct_chat(*, chat_id: int, sender_id: int | None) -> None:
    """Apply the same private-DM policy to receipt capture arguments."""
    if chat_id <= 0:
        raise _refuse(
            "Telegram chat ID must be a positive private-DM identifier; "
            "group and supergroup chats are refused."
        )
    if sender_id is None:
        raise _refuse("Receipt capture must identify the private-DM sender.")
    if sender_id != chat_id:
        raise _refuse("Telegram sender identity does not match the private chat identity.")


__all__ = [
    "validate_private_direct_chat",
    "validate_private_direct_message",
]
