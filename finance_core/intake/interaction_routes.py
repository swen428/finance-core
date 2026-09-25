"""Frozen route for a durably captured private Telegram message.

Classification is made once under the same write transaction as raw source
and job creation. Replay reads this row; a later session cannot reinterpret it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.staging_guard import require_staging_database

_CARD = re.compile(r"^d1card_[0-9a-f]{32}$")
_CARD_LINE = re.compile(r"^[ \t]*(?:card[ \t]+ref|资料卡编号)[ \t]*[:：][ \t]*(\S+)", re.I | re.M)
_CARD_LIKE = re.compile(
    r"^[ \t]*(?:资料卡编号|金额|币种|日期|商户|描述|分类|card[ \t]+ref|"
    r"amount|currency|date|merchant|description|category)[ \t]*[:：]",
    re.I | re.M,
)
_FIELD_ALIASES = {
    "amount": "amount", "金额": "amount", "currency": "currency", "币种": "currency",
    "transaction_date": "transaction_date", "date": "transaction_date", "日期": "transaction_date",
    "merchant": "merchant", "商户": "merchant", "description": "description", "描述": "description",
    "category": "category", "分类": "category",
}


class InteractionRouteConflictError(ValueError):
    """A replay disagrees with frozen source, identity or route evidence."""


def _framed_digest(domain: str, *fields: str) -> str:
    material = domain.encode("ascii") + b"\0" + len(fields).to_bytes(4, "big")
    for field in fields:
        encoded = field.encode("utf-8")
        material += len(encoded).to_bytes(4, "big") + encoded
    return hashlib.sha256(material).hexdigest()


def get_interaction_route(conn: sqlite3.Connection, job_public_id: str) -> dict[str, Any] | None:
    require_staging_database(conn)
    cursor = conn.execute(
        "SELECT * FROM finance_capture_interaction_routes WHERE job_public_id = ?",
        (job_public_id,),
    )
    row = cursor.fetchone()
    return None if row is None else dict(zip((c[0] for c in cursor.description), row, strict=True))


def _historical_guided_session(
    conn: sqlite3.Connection, context: HumanActionContext, message_id: int
) -> str | None:
    rows = conn.execute(
        "SELECT DISTINCT s.session_public_id FROM openclaw_guided_edit_sessions s "
        "LEFT JOIN openclaw_guided_edit_events e ON e.session_id = s.id "
        "WHERE s.authenticated_actor_id = ? AND s.channel_account_id = ? "
        "AND s.channel_conversation_id = ? AND s.conversation_binding_id = ? "
        "AND (s.completed_message_id = ? OR e.telegram_message_id = ?) LIMIT 2",
        (context.actor_id, context.account_id, context.conversation_id,
         context.binding_id, message_id, message_id),
    ).fetchall()
    if len(rows) > 1:
        raise InteractionRouteConflictError("Historical guided message has ambiguous sessions")
    return None if not rows else str(rows[0][0])


def classify_interaction(
    conn: sqlite3.Connection, *, text: str, context: HumanActionContext, message_id: int
) -> dict[str, Any]:
    """Core-owned precedence: whole card, historical guided, active guided, intake."""
    # A visible D1 card label always blocks ordinary expense parsing, even
    # when malformed. Its guarded D1 handler decides material validity later.
    card_lines = _CARD_LINE.findall(text)
    if card_lines or _CARD_LIKE.search(text):
        if len(card_lines) != 1 or _CARD.fullmatch(card_lines[0]) is None:
            return {"route_kind": "control_refused", "refusal_code": "invalid_whole_card"}
        card = card_lines[0]
        operation = "d1op_" + _framed_digest(
            "d1-plugin-card-operation-v1", context.account_id,
            context.conversation_id, context.binding_id, str(message_id), card,
        )[:32]
        return {"route_kind": "whole_card", "card_generation_public_id": card,
                "operation_key": operation}

    historical = _historical_guided_session(conn, context, message_id)
    session = conn.execute(
        "SELECT session_public_id FROM openclaw_guided_edit_sessions "
        "WHERE authenticated_actor_id = ? AND channel_account_id = ? "
        "AND channel_conversation_id = ? AND conversation_binding_id = ? "
        "AND status = 'active'",
        (context.actor_id, context.account_id, context.conversation_id, context.binding_id),
    ).fetchone()
    session_id = historical or (None if session is None else str(session[0]))
    value = text.strip()
    if session_id is not None:
        if value == "完成":
            return {"route_kind": "guided_complete", "guided_session_public_id": session_id,
                    "operation_key": f"bridge-guided-edit-complete:{session_id}:{message_id}"}
        if value.count("=") == 1:
            alias, field_value = (part.strip() for part in value.split("=", 1))
            field = _FIELD_ALIASES.get(alias.lower())
            if field is not None and field_value and len(field_value.encode("utf-8")) <= 1024:
                if not any(ord(char) < 32 or ord(char) == 127 for char in field_value):
                    return {
                        "route_kind": "guided_update", "guided_session_public_id": session_id,
                        "operation_key": f"bridge-guided-edit-update:{session_id}:{message_id}",
                        "field_name": field,
                        "field_value_json": json.dumps(field_value, ensure_ascii=False),
                    }
        return {"route_kind": "control_refused", "refusal_code": "invalid_guided_control",
                "guided_session_public_id": session_id}
    if value == "完成" or value.count("=") == 1:
        return {"route_kind": "control_refused", "refusal_code": "no_guided_session"}
    return {"route_kind": "initial_intake"}


def freeze_interaction_route(
    conn: sqlite3.Connection, *, job_public_id: str, text: str,
    context: HumanActionContext, message_id: int,
) -> dict[str, Any]:
    require_staging_database(conn)
    if not conn.in_transaction:
        raise RuntimeError("Interaction route requires the capture transaction")
    if get_interaction_route(conn, job_public_id) is not None:
        raise InteractionRouteConflictError("Interaction route already exists")
    route = classify_interaction(conn, text=text, context=context, message_id=message_id)
    conn.execute(
        "INSERT INTO finance_capture_interaction_routes "
        "(job_public_id, route_kind, raw_text_sha256, authenticated_actor_id, "
        "telegram_account_id, telegram_conversation_id, conversation_binding_id, "
        "telegram_message_id, card_generation_public_id, guided_session_public_id, "
        "operation_key, field_name, field_value_json, refusal_code) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_public_id, route["route_kind"], hashlib.sha256(text.encode()).hexdigest(),
         context.actor_id, context.account_id, context.conversation_id, context.binding_id,
         message_id, route.get("card_generation_public_id"), route.get("guided_session_public_id"),
         route.get("operation_key"), route.get("field_name"), route.get("field_value_json"),
         route.get("refusal_code")),
    )
    saved = get_interaction_route(conn, job_public_id)
    assert saved is not None
    return saved


def require_replayed_interaction_route(
    conn: sqlite3.Connection, *, job_public_id: str, text: str,
    context: HumanActionContext, message_id: int,
) -> dict[str, Any]:
    route = get_interaction_route(conn, job_public_id)
    if route is None or any((
        route["raw_text_sha256"] != hashlib.sha256(text.encode()).hexdigest(),
        route["authenticated_actor_id"] != context.actor_id,
        route["telegram_account_id"] != context.account_id,
        route["telegram_conversation_id"] != context.conversation_id,
        route["conversation_binding_id"] != context.binding_id,
        route["telegram_message_id"] != message_id,
    )):
        raise InteractionRouteConflictError("Replay differs from the frozen interaction route")
    return route
