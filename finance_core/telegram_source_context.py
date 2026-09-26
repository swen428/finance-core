"""Immutable Telegram capture context used by D2 initial proposal cards."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass


class TelegramSourceContextError(ValueError):
    """The durable Telegram capture context is missing or conflicts."""


@dataclass(frozen=True)
class TelegramSourceContext:
    authenticated_actor_id: str
    account_id: str
    conversation_id: str
    binding_id: str
    message_id: str


def telegram_source_identity_sha256(context: TelegramSourceContext) -> str:
    material = json.dumps(
        {
            "authenticated_actor_id": context.authenticated_actor_id,
            "conversation_binding_id": context.binding_id,
            "source_message_id": context.message_id,
            "telegram_account_id": context.account_id,
            "telegram_conversation_id": context.conversation_id,
            "version": "finance_d2_telegram_source_context_v1",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _validated(context: TelegramSourceContext) -> TelegramSourceContext:
    values = (
        context.authenticated_actor_id,
        context.account_id,
        context.conversation_id,
        context.binding_id,
        context.message_id,
    )
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise TelegramSourceContextError("Telegram source context is incomplete")
    return TelegramSourceContext(*(value.strip() for value in values))


def record_telegram_source_context(
    conn: sqlite3.Connection,
    *,
    raw_intake_record_id: int,
    context: TelegramSourceContext,
    captured_at: str,
) -> str:
    """Persist an exact capture-time context inside the caller transaction."""
    context = _validated(context)
    raw = conn.execute(
        "SELECT source_channel, external_source_id, source_message_id "
        "FROM raw_intake_records WHERE id = ?",
        (raw_intake_record_id,),
    ).fetchone()
    expected_external = f"telegram:{context.conversation_id}:{context.message_id}"
    if (
        raw is None
        or raw["source_channel"] != "telegram"
        or str(raw["source_message_id"] or "") != context.message_id
        or str(raw["external_source_id"] or "") != expected_external
    ):
        raise TelegramSourceContextError("Telegram source context does not match raw intake")
    digest = telegram_source_identity_sha256(context)
    rows = conn.execute(
        "SELECT * FROM d2_telegram_source_contexts "
        "WHERE raw_intake_record_id = ? OR "
        "(telegram_account_id = ? AND telegram_conversation_id = ? "
        "AND source_message_id = ?)",
        (
            raw_intake_record_id,
            context.account_id,
            context.conversation_id,
            context.message_id,
        ),
    ).fetchall()
    if not rows:
        conn.execute(
            "INSERT INTO d2_telegram_source_contexts "
            "(raw_intake_record_id, authenticated_actor_id, telegram_account_id, "
            "telegram_conversation_id, conversation_binding_id, source_message_id, "
            "source_identity_sha256, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                raw_intake_record_id,
                context.authenticated_actor_id,
                context.account_id,
                context.conversation_id,
                context.binding_id,
                context.message_id,
                digest,
                captured_at,
            ),
        )
        return digest
    if len(rows) != 1:
        raise TelegramSourceContextError("Telegram source context identity collision")
    row = rows[0]
    actual = (
        int(row["raw_intake_record_id"]),
        str(row["authenticated_actor_id"]),
        str(row["telegram_account_id"]),
        str(row["telegram_conversation_id"]),
        str(row["conversation_binding_id"]),
        str(row["source_message_id"]),
    )
    expected = (
        raw_intake_record_id,
        context.authenticated_actor_id,
        context.account_id,
        context.conversation_id,
        context.binding_id,
        context.message_id,
    )
    if actual != expected or not hmac.compare_digest(str(row["source_identity_sha256"]), digest):
        raise TelegramSourceContextError("Telegram source context identity collision")
    return digest


def require_telegram_source_context(
    conn: sqlite3.Connection,
    *,
    raw_intake_record_id: int,
    context: TelegramSourceContext,
) -> str:
    """Return the frozen digest only when the complete capture context matches."""
    context = _validated(context)
    row = conn.execute(
        "SELECT * FROM d2_telegram_source_contexts WHERE raw_intake_record_id = ?",
        (raw_intake_record_id,),
    ).fetchone()
    digest = telegram_source_identity_sha256(context)
    if row is None:
        raise TelegramSourceContextError("Telegram source context is unavailable")
    actual = (
        str(row["authenticated_actor_id"]),
        str(row["telegram_account_id"]),
        str(row["telegram_conversation_id"]),
        str(row["conversation_binding_id"]),
        str(row["source_message_id"]),
    )
    expected = (
        context.authenticated_actor_id,
        context.account_id,
        context.conversation_id,
        context.binding_id,
        context.message_id,
    )
    if actual != expected or not hmac.compare_digest(str(row["source_identity_sha256"]), digest):
        raise TelegramSourceContextError("Telegram source context mismatch")
    return digest


def has_telegram_source_context(
    conn: sqlite3.Connection, *, raw_intake_record_id: int
) -> bool:
    """Whether the intake has a frozen capture identity that replay must supply."""
    row = conn.execute(
        "SELECT 1 FROM d2_telegram_source_contexts WHERE raw_intake_record_id = ?",
        (raw_intake_record_id,),
    ).fetchone()
    return row is not None


__all__ = [
    "TelegramSourceContext",
    "TelegramSourceContextError",
    "has_telegram_source_context",
    "record_telegram_source_context",
    "require_telegram_source_context",
    "telegram_source_identity_sha256",
]
