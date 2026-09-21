"""Telegram Bot API text-message update intake adapter.

Validates a standard Telegram Bot API text-message update payload,
maps it into a validated immutable DTO, and routes it through the
existing raw-text intake service and parser-proposal workflow.

This adapter is a pure validation and persistence boundary. It does
not make network calls, does not confirm or finalize financial
records, and does not introduce Telegram SDK dependencies.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from finance_core.intake.raw_text_service import process_raw_text_input

_TELEGRAM_EPOCH = 1262304000  # 2010-01-01T00:00:00 UTC


@dataclass(frozen=True)
class TelegramTextUpdate:
    """Validated, immutable representation of a Telegram text-message update.

    All fields are required unless noted optional.  The dataclass is
    frozen so callers cannot accidentally mutate a validated update.
    """

    update_id: int | None
    message_id: int
    chat_id: int
    date: int  # Unix timestamp as received from Telegram
    text: str  # Preserved exactly -- no trimming, normalization, or rewriting
    sender_id: int | None = None
    _source_received_at: str = field(repr=False, default="")

    @property
    def source_received_at(self) -> str:
        """Telegram message date converted to timezone-aware UTC ISO-8601.

        Pre-computed during validation; repeated access cannot produce
        a platform-dependent conversion failure.
        """
        return self._source_received_at

    @property
    def source_metadata(self) -> dict[str, str]:
        """Source metadata mapping consumed by the raw-intake repository.

        Returns a fresh ``dict`` on every call (defensive copy).
        """
        metadata: dict[str, str] = {
            "chat_id": str(self.chat_id),
            "message_id": str(self.message_id),
            "source_message_id": str(self.message_id),
            "source_received_at": self.source_received_at,
            "telegram_message_date": str(self.date),
        }
        if self.update_id is not None:
            metadata["telegram_update_id"] = str(self.update_id)
        if self.sender_id is not None:
            metadata["sender_id"] = str(self.sender_id)
        return metadata


class TelegramTextUpdateValidationError(ValueError):
    """A Telegram update payload failed validation with a stable reason code."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code: str = reason_code


def validate_telegram_text_update(payload: Any) -> TelegramTextUpdate:
    """Validate and map a Telegram Bot API update into a TelegramTextUpdate.

    Only standard text-message updates are supported.  Unsupported
    update forms fail with a stable ``reason_code``.

    Raises:
        TelegramTextUpdateValidationError: validation failure.
    """
    if not isinstance(payload, Mapping):
        raise TelegramTextUpdateValidationError(
            "Telegram update payload must be a mapping",
            reason_code="PAYLOAD_NOT_MAPPING",
        )

    _reject_unsupported_update_type(payload)

    if "message" not in payload:
        raise TelegramTextUpdateValidationError(
            "Missing required 'message' field in update payload",
            reason_code="MISSING_MESSAGE",
        )
    message = payload["message"]

    update_id = _validate_integer_field(payload, "update_id", non_negative=True)

    if not isinstance(message, Mapping):
        raise TelegramTextUpdateValidationError(
            "'message' must be a mapping",
            reason_code="MESSAGE_NOT_MAPPING",
        )

    message_id = _validate_integer_field(message, "message_id", positive=True)

    chat_id = _validate_chat(message)

    date, utc_iso = _validate_message_date(message)

    text = _validate_text(message)

    sender_id = _validate_optional_sender(message)

    return TelegramTextUpdate(
        update_id=update_id,
        message_id=message_id,
        chat_id=chat_id,
        date=date,
        text=text,
        sender_id=sender_id,
        _source_received_at=utc_iso,
    )


def process_telegram_text_update(
    conn: sqlite3.Connection,
    payload: Any,
    *,
    received_at: datetime | str | None = None,
    persistence_effect: Callable[[sqlite3.Connection, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Validate a raw Telegram text-message update payload and persist.

    This is the sole public persistence entry point.  Validation runs
    internally on every call; there is no unchecked persistence path.

    Returns the same result shape as ``process_raw_text_input``:
    ``{"intake": ..., "parser_output": ..., "proposal": ...}``.
    """
    validated = validate_telegram_text_update(payload)
    return _persist_validated_telegram_text_update(
        conn,
        validated,
        received_at=received_at,
        persistence_effect=persistence_effect,
    )


def validate_openclaw_telegram_text_message(payload: Any) -> TelegramTextUpdate:
    """Validate the normalized Telegram message facts exposed by OpenClaw.

    OpenClaw's pinned public ``inbound_claim`` event does not expose the Bot
    API ``update_id``.  This boundary therefore preserves the available
    message/chat/sender/time facts without inventing an update identifier.
    """
    if not isinstance(payload, Mapping):
        raise TelegramTextUpdateValidationError(
            "OpenClaw Telegram message must be a mapping",
            reason_code="MESSAGE_NOT_MAPPING",
        )
    message = payload
    message_id = _validate_integer_field(message, "message_id", positive=True)
    chat_id = _validate_chat(message)
    date, utc_iso = _validate_message_date(message)
    text = _validate_text(message)
    sender_id = _validate_optional_sender(message)
    return TelegramTextUpdate(
        update_id=None,
        message_id=message_id,
        chat_id=chat_id,
        date=date,
        text=text,
        sender_id=sender_id,
        _source_received_at=utc_iso,
    )


def process_openclaw_telegram_text_message(
    conn: sqlite3.Connection,
    payload: Any,
    *,
    received_at: datetime | str | None = None,
    persistence_effect: Callable[[sqlite3.Connection, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Persist one validated OpenClaw-normalized Telegram message."""
    validated = validate_openclaw_telegram_text_message(payload)
    return _persist_validated_telegram_text_update(
        conn,
        validated,
        received_at=received_at,
        persistence_effect=persistence_effect,
    )


def _persist_validated_telegram_text_update(
    conn: sqlite3.Connection,
    telegram_update: TelegramTextUpdate,
    *,
    received_at: datetime | str | None = None,
    persistence_effect: Callable[[sqlite3.Connection, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Persist an already-validated TelegramTextUpdate.

    Private: callers must validate through ``process_telegram_text_update``
    or ``validate_telegram_text_update`` before calling this helper.
    """
    return process_raw_text_input(
        conn,
        telegram_update.text,
        source_type="telegram_text",
        source_channel="telegram",
        source_metadata=telegram_update.source_metadata,
        received_at=received_at,
        persistence_effect=persistence_effect,
    )


# ---------------------------------------------------------------------------
#  Internal helpers
# ---------------------------------------------------------------------------

_UNSUPPORTED_UPDATE_KEYS = frozenset(
    {
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "callback_query",
        "inline_query",
        "poll",
        "poll_answer",
        "my_chat_member",
        "chat_member",
        "chat_join_request",
        "pre_checkout_query",
        "shipping_query",
    }
)


def _reject_unsupported_update_type(payload: Mapping[str, Any]) -> None:
    for key in _UNSUPPORTED_UPDATE_KEYS:
        if key in payload:
            raise TelegramTextUpdateValidationError(
                f"Unsupported update type: '{key}'",
                reason_code="UNSUPPORTED_UPDATE_TYPE",
            )


def _validate_integer_field(
    container: Mapping[str, Any],
    field: str,
    *,
    non_negative: bool = False,
    positive: bool = False,
) -> int:
    value = container.get(field)
    if value is None:
        raise TelegramTextUpdateValidationError(
            f"Missing required field '{field}'",
            reason_code=f"MISSING_{field.upper()}",
        )
    if isinstance(value, bool):
        raise TelegramTextUpdateValidationError(
            f"'{field}' must be an integer, not a boolean",
            reason_code=f"{field.upper()}_BOOL",
        )
    if not isinstance(value, int):
        raise TelegramTextUpdateValidationError(
            f"'{field}' must be an integer, got {type(value).__name__}",
            reason_code=f"{field.upper()}_TYPE",
        )
    if positive and value <= 0:
        raise TelegramTextUpdateValidationError(
            f"'{field}' must be a positive integer",
            reason_code=f"{field.upper()}_NOT_POSITIVE",
        )
    if non_negative and value < 0:
        raise TelegramTextUpdateValidationError(
            f"'{field}' must be a non-negative integer",
            reason_code=f"{field.upper()}_NEGATIVE",
        )
    return value


def _validate_chat(message: Mapping[str, Any]) -> int:
    chat = message.get("chat")
    if chat is None:
        raise TelegramTextUpdateValidationError(
            "Missing required 'chat' object in message",
            reason_code="MISSING_CHAT",
        )
    if not isinstance(chat, Mapping):
        raise TelegramTextUpdateValidationError(
            "'chat' must be a mapping",
            reason_code="CHAT_NOT_MAPPING",
        )
    return _validate_integer_field(chat, "id")


def _validate_message_date(message: Mapping[str, Any]) -> tuple[int, str]:
    """Validate and convert the message date.

    Returns a (raw_timestamp, utc_iso_string) pair.  The UTC ISO
    string is computed eagerly so that repeated access cannot produce
    a new platform-dependent failure.
    """
    date = message.get("date")
    if date is None:
        raise TelegramTextUpdateValidationError(
            "Missing required 'date' in message",
            reason_code="MISSING_MESSAGE_DATE",
        )
    if isinstance(date, bool):
        raise TelegramTextUpdateValidationError(
            "Message 'date' must be an integer, not a boolean",
            reason_code="MESSAGE_DATE_BOOL",
        )
    if not isinstance(date, int):
        raise TelegramTextUpdateValidationError(
            f"Message 'date' must be an integer Unix timestamp, got {type(date).__name__}",
            reason_code="MESSAGE_DATE_TYPE",
        )
    if date < 0:
        raise TelegramTextUpdateValidationError(
            "Message 'date' must be non-negative",
            reason_code="MESSAGE_DATE_NEGATIVE",
        )
    # Reject timestamps before Telegram's existence (2010-01-01)
    if date < _TELEGRAM_EPOCH:
        raise TelegramTextUpdateValidationError(
            f"Message 'date' {date} is outside the valid Telegram timestamp range",
            reason_code="MESSAGE_DATE_OUT_OF_RANGE",
        )
    try:
        utc_iso = datetime.fromtimestamp(date, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        raise TelegramTextUpdateValidationError(
            f"Message 'date' {date} cannot be converted to a valid UTC timestamp",
            reason_code="MESSAGE_DATE_OUT_OF_RANGE",
        )
    return date, utc_iso


def _validate_text(message: Mapping[str, Any]) -> str:
    text = message.get("text")
    if text is None:
        # Distinguish truly missing text from media-only messages for a
        # more helpful error when we can detect it.
        if any(k in message for k in ("photo", "document", "audio", "voice", "video")):
            raise TelegramTextUpdateValidationError(
                "Media-only message without text is not supported",
                reason_code="MEDIA_WITHOUT_TEXT",
            )
        raise TelegramTextUpdateValidationError(
            "Missing required 'text' field in message",
            reason_code="MISSING_TEXT",
        )
    if not isinstance(text, str):
        raise TelegramTextUpdateValidationError(
            f"'text' must be a string, got {type(text).__name__}",
            reason_code="TEXT_NOT_STRING",
        )
    if not text.strip():
        raise TelegramTextUpdateValidationError(
            "Message text is empty or whitespace-only",
            reason_code="TEXT_EMPTY_OR_WHITESPACE",
        )
    return text


def _validate_optional_sender(message: Mapping[str, Any]) -> int | None:
    """Validate the optional sender ``from`` field.

    Returns the sender integer ID or ``None`` when the field is absent.
    Malformed ``from`` values are rejected with sender-specific reason
    codes, including when ``from`` is explicitly ``None``.
    """
    if "from" not in message:
        return None
    from_field = message["from"]
    if not isinstance(from_field, Mapping):
        raise TelegramTextUpdateValidationError(
            f"'from' must be a mapping, got {type(from_field).__name__}",
            reason_code="SENDER_NOT_MAPPING",
        )
    id_value = from_field.get("id")
    if id_value is None:
        raise TelegramTextUpdateValidationError(
            "Missing required 'id' in sender 'from' field",
            reason_code="MISSING_SENDER_ID",
        )
    if isinstance(id_value, bool):
        raise TelegramTextUpdateValidationError(
            "Sender 'id' must be an integer, not a boolean",
            reason_code="SENDER_ID_BOOL",
        )
    if not isinstance(id_value, int):
        raise TelegramTextUpdateValidationError(
            f"Sender 'id' must be an integer, got {type(id_value).__name__}",
            reason_code="SENDER_ID_TYPE",
        )
    return id_value
