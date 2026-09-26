"""Frozen route for a durably captured private Telegram message.

Classification is made once under the same write transaction as raw source
and job creation. Replay reads this row; a later session cannot reinterpret it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime
from typing import Any, Protocol

from finance_core.parser_proposals.human_drafts import (
    parse_human_draft_card_structure_for_routing,
)
from finance_core.staging_guard import require_staging_database

_CARD_LABEL_START = re.compile(
    r"^(?:资料卡编号|金额|币种|日期|商户|描述|分类|card\s*ref|"
    r"amount|currency|transaction_date|date|merchant|description|category)"
    r"(?=$|\s|[:：])",
    re.I,
)
_FIELD_ALIASES = {
    "amount": "amount",
    "金额": "amount",
    "currency": "currency",
    "币种": "currency",
    "transaction_date": "transaction_date",
    "date": "transaction_date",
    "日期": "transaction_date",
    "merchant": "merchant",
    "商户": "merchant",
    "description": "description",
    "描述": "description",
    "category": "category",
    "分类": "category",
}


def _normalized_control_text(text: str) -> str | None:
    """Normalize only line endings for classification; retain original evidence."""
    for char in text:
        if char in "\r\n\t":
            continue
        category = unicodedata.category(char)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            return None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _guided_shape(normalized: str) -> tuple[str, str | None, str | None]:
    if "\n" in normalized:
        return "invalid", None, None
    value = normalized.strip(" \t")
    if value == "完成":
        return "complete", None, None
    if value.count("=") != 1 or "＝" in value:
        return "invalid", None, None
    alias, field_value = (part.strip(" \t") for part in value.split("=", 1))
    field = _FIELD_ALIASES.get(alias.lower())
    if field is None or not field_value or len(field_value.encode("utf-8")) > 1024:
        return "invalid", None, None
    if any(
        unicodedata.category(char).startswith("C")
        or unicodedata.category(char) in {"Zl", "Zp"}
        or (unicodedata.category(char) == "Zs" and char != " ")
        for char in field_value
    ):
        return "invalid", None, None
    return "update", field, field_value


def classify_control_text_shape(text: str) -> tuple[str, str | None]:
    """Classify control candidates before they can acquire ordinary parser rights.

    The shape decision is shared by text routing and receipt-caption admission.
    D1 and guided handlers still decide whether a candidate is actionable.
    """
    normalized = _normalized_control_text(text)
    if normalized is None:
        return "invalid", None
    lines = (line.strip(" \t") for line in normalized.split("\n"))
    card_candidate = "d1card_" in normalized.casefold() or any(
        _CARD_LABEL_START.match(line) is not None for line in lines
    )
    guided_candidate = (
        "=" in normalized
        or "＝" in normalized
        or any(line.strip(" \t") == "完成" for line in normalized.split("\n"))
    )
    if card_candidate and guided_candidate:
        return "ambiguous", normalized
    if card_candidate:
        return "card", normalized
    if guided_candidate:
        return "guided", normalized
    return "ordinary", normalized


class InteractionRouteConflictError(ValueError):
    """A replay disagrees with frozen source, identity or route evidence."""


def begin_interaction_capture(conn: sqlite3.Connection) -> None:
    """Reserve the route decision and its source/job writes in one write unit."""
    require_staging_database(conn)
    conn.execute("BEGIN IMMEDIATE")


def mark_control_refusal(
    conn: sqlite3.Connection, *, job_public_id: str, refusal_code: str
) -> None:
    require_staging_database(conn)
    if not conn.in_transaction:
        raise RuntimeError("Control refusal requires the capture transaction")
    conn.execute(
        "UPDATE finance_capture_jobs SET status = 'needs_attention', last_error = ? "
        "WHERE public_id = ?",
        (refusal_code, job_public_id),
    )


def find_interaction_route_job(
    conn: sqlite3.Connection,
    *,
    context: InteractionContext,
    message_id: int | None = None,
    operation_key: str | None = None,
) -> str | None:
    """Find one immutable route by authenticated source and one selector."""
    require_staging_database(conn)
    if (message_id is None) == (operation_key is None):
        raise ValueError("Provide exactly one route selector")
    predicate = "r.telegram_message_id = ?" if message_id is not None else "r.operation_key = ?"
    key = message_id if message_id is not None else operation_key
    rows = conn.execute(
        "SELECT r.job_public_id FROM finance_capture_interaction_routes r "
        "WHERE r.authenticated_actor_id = ? AND r.telegram_account_id = ? "
        "AND r.telegram_conversation_id = ? AND r.conversation_binding_id = ? "
        f"AND {predicate}",
        (context.actor_id, context.account_id, context.conversation_id, context.binding_id, key),
    ).fetchall()
    if len(rows) > 1:
        raise InteractionRouteConflictError("Original interaction is ambiguous")
    return None if not rows else str(rows[0][0])


class InteractionContext(Protocol):
    """The authenticated source fields needed for routing, without a platform import."""

    @property
    def actor_id(self) -> str: ...

    @property
    def account_id(self) -> str: ...

    @property
    def conversation_id(self) -> str: ...

    @property
    def binding_id(self) -> str: ...


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


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
    conn: sqlite3.Connection, context: InteractionContext, message_id: int
) -> str | None:
    rows = conn.execute(
        "SELECT DISTINCT s.session_public_id FROM openclaw_guided_edit_sessions s "
        "LEFT JOIN openclaw_guided_edit_events e ON e.session_id = s.id "
        "WHERE s.authenticated_actor_id = ? AND s.channel_account_id = ? "
        "AND s.channel_conversation_id = ? AND s.conversation_binding_id = ? "
        "AND (s.completed_message_id = ? OR e.telegram_message_id = ?) LIMIT 2",
        (
            context.actor_id,
            context.account_id,
            context.conversation_id,
            context.binding_id,
            message_id,
            message_id,
        ),
    ).fetchall()
    if len(rows) > 1:
        raise InteractionRouteConflictError("Historical guided message has ambiguous sessions")
    return None if not rows else str(rows[0][0])


def _historical_guided_route(
    conn: sqlite3.Connection, *, session_id: str, message_id: int, normalized: str
) -> dict[str, Any]:
    refusal = {
        "route_kind": "control_refused",
        "refusal_code": "historical_guided_mismatch",
        "guided_session_public_id": session_id,
    }
    rows = conn.execute(
        "SELECT e.event_type, e.operation_key, e.field_name, e.field_value_json, "
        "s.completed_message_id, s.pending_message_id, s.pending_operation_key, "
        "s.pending_field_name, s.pending_field_value_json "
        "FROM openclaw_guided_edit_events e "
        "JOIN openclaw_guided_edit_sessions s ON s.id = e.session_id "
        "WHERE s.session_public_id = ? AND e.telegram_message_id = ? "
        "AND e.event_type IN ('update_requested', 'completed')",
        (session_id, message_id),
    ).fetchall()
    if len(rows) != 1:
        return refusal
    (
        event_type,
        original_key,
        original_field,
        original_value_json,
        completed_id,
        pending_id,
        pending_key,
        pending_field,
        pending_value_json,
    ) = rows[0]
    kind, field, value = _guided_shape(normalized)
    if event_type == "completed":
        if kind != "complete" or completed_id != message_id:
            return refusal
        return {
            "route_kind": "guided_complete",
            "guided_session_public_id": session_id,
            "operation_key": f"bridge-guided-edit-complete:{session_id}:{message_id}",
        }
    operation_key = f"bridge-guided-edit-update:{session_id}:{message_id}"
    if kind != "update" or field != original_field or not original_key:
        return refusal
    try:
        original_value = json.loads(str(original_value_json))
    except (TypeError, ValueError):
        return refusal
    if value != original_value:
        return refusal
    # A route key identifies the Telegram message; the execution key recorded
    # by the guided edit authority identifies the proposal/version operation.
    # Bind recovery to the original pending request or its persisted outcome.
    pending_matches = (
        pending_id == message_id
        and pending_key == original_key
        and pending_field == original_field
        and pending_value_json == original_value_json
    )
    settled = conn.execute(
        "SELECT e.operation_key, e.field_name, e.field_value_json "
        "FROM openclaw_guided_edit_events e "
        "JOIN openclaw_guided_edit_sessions s ON s.id = e.session_id "
        "WHERE s.session_public_id = ? AND e.telegram_message_id = ? "
        "AND e.event_type IN ('update_applied', 'update_refused')",
        (session_id, message_id),
    ).fetchall()
    settled_matches = len(settled) == 1 and tuple(settled[0]) == (
        original_key,
        original_field,
        original_value_json,
    )
    if not pending_matches and not settled_matches:
        return refusal
    return {
        "route_kind": "guided_update",
        "guided_session_public_id": session_id,
        "operation_key": operation_key,
        "d1_compatibility_operation_public_id": _guided_d1_operation_id(session_id, message_id),
        "field_name": field,
        "field_value_json": json.dumps(value, ensure_ascii=False),
    }


def _guided_d1_operation_id(session_id: str, message_id: int) -> str:
    """Freeze the D1 compatibility operation separately from the route key."""
    material = "\x00".join(("d1-guided-edit-compatibility-v1", session_id, str(message_id)))
    return "d1op_" + hashlib.sha256(material.encode()).hexdigest()[:32]


def classify_interaction(
    conn: sqlite3.Connection, *, text: str, context: InteractionContext, message_id: int
) -> dict[str, Any]:
    """Freeze one Core-owned interpretation under the source/job transaction."""
    shape, normalized = classify_control_text_shape(text)
    if shape == "invalid":
        return {"route_kind": "control_refused", "refusal_code": "invalid_control_text"}
    if shape == "ambiguous":
        return {"route_kind": "control_refused", "refusal_code": "ambiguous_control"}
    assert normalized is not None
    try:
        historical = _historical_guided_session(conn, context, message_id)
    except InteractionRouteConflictError:
        return {"route_kind": "control_refused", "refusal_code": "ambiguous_guided_history"}
    if historical is not None:
        if shape == "card":
            return {"route_kind": "control_refused", "refusal_code": "ambiguous_control"}
        return _historical_guided_route(
            conn, session_id=historical, message_id=message_id, normalized=normalized
        )
    if shape == "card":
        card = parse_human_draft_card_structure_for_routing(normalized)
        if card is None:
            return {"route_kind": "control_refused", "refusal_code": "invalid_whole_card"}
        operation = (
            "d1op_"
            + _framed_digest(
                "d1-plugin-card-operation-v1",
                context.account_id,
                context.conversation_id,
                context.binding_id,
                str(message_id),
                card,
            )[:32]
        )
        return {
            "route_kind": "whole_card",
            "card_generation_public_id": card,
            "operation_key": operation,
        }

    session = conn.execute(
        "SELECT session_public_id FROM openclaw_guided_edit_sessions "
        "WHERE authenticated_actor_id = ? AND channel_account_id = ? "
        "AND channel_conversation_id = ? AND conversation_binding_id = ? "
        "AND status = 'active' AND expires_at > ?",
        (
            context.actor_id,
            context.account_id,
            context.conversation_id,
            context.binding_id,
            _now_epoch(),
        ),
    ).fetchone()
    session_id = None if session is None else str(session[0])
    if session_id is not None:
        kind, field, field_value = _guided_shape(normalized)
        if kind == "complete":
            return {
                "route_kind": "guided_complete",
                "guided_session_public_id": session_id,
                "operation_key": f"bridge-guided-edit-complete:{session_id}:{message_id}",
            }
        if kind == "update" and field is not None and field_value is not None:
            return {
                "route_kind": "guided_update",
                "guided_session_public_id": session_id,
                "operation_key": f"bridge-guided-edit-update:{session_id}:{message_id}",
                "d1_compatibility_operation_public_id": _guided_d1_operation_id(
                    session_id, message_id
                ),
                "field_name": field,
                "field_value_json": json.dumps(field_value, ensure_ascii=False),
            }
        return {
            "route_kind": "control_refused",
            "refusal_code": "invalid_guided_control",
            "guided_session_public_id": session_id,
        }
    # Every control-shaped input is refused without an active session, even
    # when its field alias or syntax is malformed.
    if shape == "guided":
        return {"route_kind": "control_refused", "refusal_code": "no_guided_session"}
    return {"route_kind": "initial_intake"}


def freeze_interaction_route(
    conn: sqlite3.Connection,
    *,
    job_public_id: str,
    text: str,
    context: InteractionContext,
    message_id: int,
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
        "operation_key, d1_compatibility_operation_public_id, "
        "field_name, field_value_json, refusal_code) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_public_id,
            route["route_kind"],
            hashlib.sha256(text.encode()).hexdigest(),
            context.actor_id,
            context.account_id,
            context.conversation_id,
            context.binding_id,
            message_id,
            route.get("card_generation_public_id"),
            route.get("guided_session_public_id"),
            route.get("operation_key"),
            route.get("d1_compatibility_operation_public_id"),
            route.get("field_name"),
            route.get("field_value_json"),
            route.get("refusal_code"),
        ),
    )
    saved = get_interaction_route(conn, job_public_id)
    assert saved is not None
    return saved


def require_replayed_interaction_route(
    conn: sqlite3.Connection,
    *,
    job_public_id: str,
    text: str,
    context: InteractionContext,
    message_id: int,
) -> dict[str, Any]:
    route = get_interaction_route(conn, job_public_id)
    if route is None or any(
        (
            route["raw_text_sha256"] != hashlib.sha256(text.encode()).hexdigest(),
            route["authenticated_actor_id"] != context.actor_id,
            route["telegram_account_id"] != context.account_id,
            route["telegram_conversation_id"] != context.conversation_id,
            route["conversation_binding_id"] != context.binding_id,
            route["telegram_message_id"] != message_id,
        )
    ):
        raise InteractionRouteConflictError("Replay differs from the frozen interaction route")
    return route
