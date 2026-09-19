"""Durable private-Telegram guided-edit session state.

This module persists only workflow authorization and recovery material. The
authoritative financial changes remain owned by parser completion and receipt
supersession services.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from finance_core.money import MoneyValidationError, normalize_currency
from finance_core.openclaw_staging_bridge import identity
from finance_core.openclaw_staging_bridge.human_actions import HumanActionContext
from finance_core.parser_proposals.completion import get_completion_by_public_id
from finance_core.parser_proposals.content_hash import (
    canonicalize_proposal_money,
    compute_effective_proposal_content_hash,
)
from finance_core.parser_proposals.effective_payload import resolve_effective_payload
from finance_core.parser_proposals.lifecycle import TERMINAL_STATUSES
from finance_core.parser_proposals.receipt_supersession import (
    get_receipt_proposal_revision_by_correction_id,
)
from finance_core.parser_proposals.repository import ParserProposalRepository, row_to_dict

ALLOWED_FIELDS = frozenset(
    {"amount", "currency", "transaction_date", "merchant", "description", "category"}
)
BRIDGE_CHANNEL = "openclaw_staging_bridge"
REVIEW_REFERENCE_MIN_REMAINING_SECONDS = 60


class GuidedEditError(RuntimeError):
    """Stable fail-closed session refusal."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def _session_public_id(reference_public_id: str) -> str:
    return f"gedit_{_digest('guided-edit-session-v1', reference_public_id)[:32]}"


def _event_public_id(session_public_id: str, sequence: int, event_type: str) -> str:
    digest = _digest("guided-edit-event-v1", session_public_id, str(sequence), event_type)
    return f"geditev_{digest[:32]}"


def _review_batch_id(session_public_id: str, completed_message_id: int, generation: int) -> str:
    return _digest(
        "guided-edit-review-generation-v1",
        session_public_id,
        str(completed_message_id),
        str(generation),
    )[:32]


def d1_compatibility_operation_public_id(session_public_id: str, message_id: int) -> str:
    digest = _digest("d1-guided-edit-compatibility-v1", session_public_id, str(message_id))
    return f"d1op_{digest[:32]}"


def active_d1_card_for_session(conn: sqlite3.Connection, session: dict[str, Any]) -> str | None:
    rows = conn.execute(
        """
        SELECT current_card_generation_public_id
        FROM parser_human_drafts
        WHERE state = 'active' AND authenticated_actor_id = ?
          AND telegram_account_id = ? AND telegram_conversation_id = ?
          AND conversation_binding_id = ?
          AND (source_parser_output_id = ? OR current_parser_output_id = ?
               OR decision_target_parser_output_id = ?)
        """,
        (
            session["authenticated_actor_id"],
            session["channel_account_id"],
            session["channel_conversation_id"],
            session["conversation_binding_id"],
            session["current_parser_output_id"],
            session["current_parser_output_id"],
            session["current_parser_output_id"],
        ),
    ).fetchall()
    if len(rows) > 1:
        raise GuidedEditError("pending_recovery")
    return None if not rows else str(rows[0][0])


def _begin(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise GuidedEditError("transaction_conflict")
    conn.execute("BEGIN IMMEDIATE")


def _row(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    value = cursor.fetchone()
    return None if value is None else row_to_dict(value, cursor.description)


def _context_matches(row: dict[str, Any], context: HumanActionContext) -> bool:
    return (
        row["authenticated_actor_id"] == context.actor_id
        and row["channel_account_id"] == context.account_id
        and row["channel_conversation_id"] == context.conversation_id
        and row["conversation_binding_id"] == context.binding_id
    )


def _proposal_state(
    conn: sqlite3.Connection, parser_output_id: int
) -> tuple[dict[str, Any], int, str]:
    proposal = ParserProposalRepository(conn).get(parser_output_id)
    if proposal is None:
        raise GuidedEditError("proposal_missing")
    _payload, _completion, version = resolve_effective_payload(conn, proposal)
    content_hash = compute_effective_proposal_content_hash(conn, {"id": parser_output_id})
    return proposal, version, content_hash


def _next_sequence(conn: sqlite3.Connection, session_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence_number), 0) + 1 "
        "FROM openclaw_guided_edit_events WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _append_event(
    conn: sqlite3.Connection,
    session: dict[str, Any],
    event_type: str,
    *,
    message_id: int | None = None,
    operation_key: str | None = None,
    field_name: str | None = None,
    field_value_json: str | None = None,
    before: tuple[int, int, str] | None = None,
    after: tuple[int, int, str] | None = None,
    refusal_code: str | None = None,
    created_at: str,
) -> None:
    sequence = _next_sequence(conn, int(session["id"]))
    conn.execute(
        """
        INSERT INTO openclaw_guided_edit_events (
            event_public_id, session_id, sequence_number, event_type,
            telegram_message_id, operation_key, field_name, field_value_json,
            before_parser_output_id, before_proposal_version, before_content_hash,
            after_parser_output_id, after_proposal_version, after_content_hash,
            refusal_code, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _event_public_id(str(session["session_public_id"]), sequence, event_type),
            session["id"],
            sequence,
            event_type,
            message_id,
            operation_key,
            field_name,
            field_value_json,
            None if before is None else before[0],
            None if before is None else before[1],
            None if before is None else before[2],
            None if after is None else after[0],
            None if after is None else after[1],
            None if after is None else after[2],
            refusal_code,
            created_at,
        ),
    )


def _pending_field_value(session: dict[str, Any]) -> str:
    try:
        value = json.loads(str(session["pending_field_value_json"]))
    except json.JSONDecodeError as exc:
        raise GuidedEditError("pending_recovery") from exc
    if not isinstance(value, str):
        raise GuidedEditError("pending_recovery")
    return value


def _pending_identity_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left["id"] == right["id"] and all(
        left[field] == right[field]
        for field in (
            "pending_message_id",
            "pending_operation_key",
            "pending_field_name",
            "pending_field_value_json",
        )
    )


def _event_matches_pending_snapshot(event: dict[str, Any], session: dict[str, Any]) -> bool:
    return all(
        event[event_field] == session[session_field]
        for event_field, session_field in (
            ("telegram_message_id", "pending_message_id"),
            ("operation_key", "pending_operation_key"),
            ("field_name", "pending_field_name"),
            ("field_value_json", "pending_field_value_json"),
        )
    )


def _pending_expected_updates(conn: sqlite3.Connection, session: dict[str, Any]) -> dict[str, str]:
    field_name = str(session["pending_field_name"])
    value = _pending_field_value(session)
    if field_name == "amount":
        proposal = ParserProposalRepository(conn).get(int(session["current_parser_output_id"]))
        if proposal is None:
            raise GuidedEditError("proposal_missing")
        payload, _completion, _version = resolve_effective_payload(conn, proposal)
        currency = payload.get("currency")
        try:
            canonical_currency = normalize_currency(currency) if isinstance(currency, str) else None
            if canonical_currency is None:
                raise MoneyValidationError("effective currency is unavailable")
            return {"amount": canonicalize_proposal_money(value, canonical_currency)}
        except MoneyValidationError as exc:
            raise GuidedEditError("pending_recovery") from exc
    if field_name == "currency":
        try:
            return {"currency": normalize_currency(value)}
        except MoneyValidationError as exc:
            raise GuidedEditError("pending_recovery") from exc
    return {field_name: value if field_name == "transaction_date" else value.strip()}


def pending_core_state(
    conn: sqlite3.Connection, session: dict[str, Any]
) -> tuple[int, int, str] | None:
    """Return an exact persisted core result without creating a new edit."""
    if session["pending_message_id"] is None:
        return None
    d1_operation_id = d1_compatibility_operation_public_id(
        str(session["session_public_id"]), int(session["pending_message_id"])
    )
    d1 = conn.execute(
        """
        SELECT operations.*, cards.parser_output_id AS card_parser_output_id,
               cards.proposal_version AS card_proposal_version,
               cards.proposal_content_hash AS card_proposal_content_hash
        FROM parser_human_draft_operations AS operations
        JOIN parser_human_draft_cards AS cards
          ON cards.card_generation_public_id = operations.result_card_generation_public_id
        WHERE operations.operation_public_id = ?
        """,
        (d1_operation_id,),
    ).fetchone()
    if d1 is not None:
        expected_fields = {str(session["pending_field_name"]): _pending_field_value(session)}
        try:
            supplied_fields = json.loads(str(d1["canonical_supplied_fields_json"]))
        except json.JSONDecodeError as exc:
            raise GuidedEditError("pending_recovery") from exc
        if (
            int(d1["telegram_message_id"]) != int(session["pending_message_id"])
            or d1["authenticated_actor_id"] != session["authenticated_actor_id"]
            or d1["telegram_account_id"] != session["channel_account_id"]
            or d1["telegram_conversation_id"] != session["channel_conversation_id"]
            or d1["conversation_binding_id"] != session["conversation_binding_id"]
            or supplied_fields != expected_fields
        ):
            raise GuidedEditError("pending_recovery")
        if d1["operation_outcome"] == "refused":
            return None
        if d1["operation_outcome"] not in {"accepted", "noop"}:
            raise GuidedEditError("pending_recovery")
        if d1["card_parser_output_id"] is None:
            return (
                int(session["current_parser_output_id"]),
                int(session["current_proposal_version"]),
                str(session["current_content_hash"]),
            )
        return (
            int(d1["card_parser_output_id"]),
            int(d1["card_proposal_version"]),
            str(d1["card_proposal_content_hash"]),
        )
    operation_key = str(session["pending_operation_key"])
    if str(session["pending_field_name"]) in {"amount", "currency"}:
        row = get_receipt_proposal_revision_by_correction_id(
            conn, identity.correction_public_id(operation_key)
        )
        if row is None:
            return None
        expected_updates = _pending_expected_updates(conn, session)
        matches = (
            int(row["superseded_parser_output_id"]) == int(session["current_parser_output_id"])
            and row["authenticated_actor_id"] == session["authenticated_actor_id"]
            and row["correction_channel"] == BRIDGE_CHANNEL
            and row["superseded_content_hash"] == session["current_content_hash"]
        )
        try:
            persisted_updates = json.loads(str(row["field_updates_json"]))
        except json.JSONDecodeError as exc:
            raise GuidedEditError("pending_recovery") from exc
        if not matches or persisted_updates != expected_updates:
            raise GuidedEditError("pending_recovery")
        return (
            int(row["replacement_parser_output_id"]),
            0,
            str(row["replacement_content_hash"]),
        )

    row = get_completion_by_public_id(conn, identity.completion_public_id(operation_key))
    if row is None:
        return None
    expected_updates = _pending_expected_updates(conn, session)
    matches = (
        int(row["parser_output_id"]) == int(session["current_parser_output_id"])
        and row["authenticated_actor_id"] == session["authenticated_actor_id"]
        and row["completion_channel"] == BRIDGE_CHANNEL
        and row["base_content_hash"] == session["current_content_hash"]
    )
    try:
        persisted_updates = json.loads(str(row["field_updates_json"]))
    except json.JSONDecodeError as exc:
        raise GuidedEditError("pending_recovery") from exc
    if not matches or persisted_updates != expected_updates:
        raise GuidedEditError("pending_recovery")
    return (
        int(row["parser_output_id"]),
        int(row["version_number"]),
        str(row["completed_content_hash"]),
    )


def _settle_pending_in_transaction(
    conn: sqlite3.Connection,
    session: dict[str, Any],
    *,
    core_state: tuple[int, int, str] | None,
    refusal_code: str = "CALLBACK_EXPIRED",
    created_at: str,
) -> dict[str, Any]:
    if not conn.in_transaction or session["pending_message_id"] is None:
        raise GuidedEditError("pending_missing")
    before = (
        int(session["current_parser_output_id"]),
        int(session["current_proposal_version"]),
        str(session["current_content_hash"]),
    )
    event_type = "update_applied" if core_state is not None else "update_refused"
    _append_event(
        conn,
        session,
        event_type,
        message_id=int(session["pending_message_id"]),
        operation_key=str(session["pending_operation_key"]),
        field_name=str(session["pending_field_name"]),
        field_value_json=str(session["pending_field_value_json"]),
        before=before,
        after=core_state,
        refusal_code=None if core_state is not None else refusal_code,
        created_at=created_at,
    )
    after = before if core_state is None else core_state
    conn.execute(
        """
        UPDATE openclaw_guided_edit_sessions
        SET current_parser_output_id = ?, current_proposal_version = ?,
            current_content_hash = ?, pending_message_id = NULL,
            pending_operation_key = NULL, pending_field_name = NULL,
            pending_field_value_json = NULL, updated_at = ?
        WHERE id = ?
        """,
        (*after, created_at, session["id"]),
    )
    settled = _row(
        conn.execute("SELECT * FROM openclaw_guided_edit_sessions WHERE id = ?", (session["id"],))
    )
    assert settled is not None
    return settled


def _reconcile_expired_pending_in_transaction(
    conn: sqlite3.Connection, session: dict[str, Any], *, created_at: str
) -> dict[str, Any]:
    if session["pending_message_id"] is None:
        return session
    core_state = pending_core_state(conn, session)
    return _settle_pending_in_transaction(
        conn, session, core_state=core_state, created_at=created_at
    )


def begin_session_in_transaction(
    conn: sqlite3.Connection,
    reference_row: dict[str, Any],
    action: str,
    *,
    initial_message_id: int,
) -> dict[str, Any]:
    """Create the session inside the human-reference redemption transaction."""
    if not conn.in_transaction or action != "edit" or reference_row["action"] != "edit":
        raise GuidedEditError("invalid_reference")
    now = _now_epoch()
    created_at = _now_text()
    session_public_id = _session_public_id(str(reference_row["reference_public_id"]))
    existing = _row(
        conn.execute(
            "SELECT * FROM openclaw_guided_edit_sessions WHERE source_reference_id = ?",
            (reference_row["id"],),
        )
    )
    if existing is not None:
        if existing["status"] != "active" or int(existing["expires_at"]) <= now:
            raise GuidedEditError("session_terminal")
        return existing

    active = _row(
        conn.execute(
            """
        SELECT * FROM openclaw_guided_edit_sessions
        WHERE channel_account_id = ? AND channel_conversation_id = ?
          AND conversation_binding_id = ? AND status = 'active'
        """,
            (
                reference_row["channel_account_id"],
                reference_row["channel_conversation_id"],
                reference_row["conversation_binding_id"],
            ),
        )
    )
    if active is not None:
        if int(active["expires_at"]) > now:
            if active["pending_message_id"] is not None:
                raise GuidedEditError("pending_recovery")
        elif active["pending_message_id"] is not None:
            active = _reconcile_expired_pending_in_transaction(conn, active, created_at=created_at)
        terminal_event = "abandoned" if int(active["expires_at"]) <= now else "superseded"
        _append_event(conn, active, terminal_event, created_at=created_at)
        conn.execute(
            "UPDATE openclaw_guided_edit_sessions SET status = ?, updated_at = ? WHERE id = ?",
            (terminal_event, created_at, active["id"]),
        )

    historical_high_water_row = conn.execute(
        """
        SELECT COALESCE(MAX(last_claimed_message_id), 0)
        FROM openclaw_guided_edit_sessions
        WHERE channel_account_id = ? AND channel_conversation_id = ?
          AND conversation_binding_id = ?
        """,
        (
            reference_row["channel_account_id"],
            reference_row["channel_conversation_id"],
            reference_row["conversation_binding_id"],
        ),
    ).fetchone()
    assert historical_high_water_row is not None
    initial_high_water = max(initial_message_id, int(historical_high_water_row[0]))

    conn.execute(
        """
        INSERT INTO openclaw_guided_edit_sessions (
            session_public_id, source_reference_id, current_parser_output_id,
            current_proposal_version, current_content_hash,
            authenticated_actor_id, channel_account_id, channel_conversation_id,
            conversation_binding_id, status, expires_at, last_claimed_message_id,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
        """,
        (
            session_public_id,
            reference_row["id"],
            reference_row["parser_output_id"],
            reference_row["proposal_version"],
            reference_row["proposal_content_hash"],
            reference_row["authenticated_actor_id"],
            reference_row["channel_account_id"],
            reference_row["channel_conversation_id"],
            reference_row["conversation_binding_id"],
            reference_row["expires_at"],
            initial_high_water,
            created_at,
            created_at,
        ),
    )
    session = _row(
        conn.execute(
            "SELECT * FROM openclaw_guided_edit_sessions WHERE session_public_id = ?",
            (session_public_id,),
        )
    )
    assert session is not None
    state = (
        int(session["current_parser_output_id"]),
        int(session["current_proposal_version"]),
        str(session["current_content_hash"]),
    )
    _append_event(conn, session, "started", before=state, after=state, created_at=created_at)
    return session


def session_for_reference(conn: sqlite3.Connection, reference_sha256: str) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            """
        SELECT sessions.* FROM openclaw_guided_edit_sessions AS sessions
        JOIN openclaw_human_action_references AS refs
          ON refs.id = sessions.source_reference_id
        WHERE refs.reference_sha256 = ?
        """,
            (reference_sha256,),
        )
    )


def get_context_session(
    conn: sqlite3.Connection,
    context: HumanActionContext,
    *,
    message_id: int | None = None,
) -> dict[str, Any] | None:
    session = None
    if message_id is not None:
        session = _row(
            conn.execute(
                """
            SELECT sessions.*, proposals.public_id AS proposal_public_id
            FROM openclaw_guided_edit_sessions AS sessions
            JOIN parser_outputs AS proposals
              ON proposals.id = sessions.current_parser_output_id
            WHERE sessions.channel_account_id = ?
              AND sessions.channel_conversation_id = ?
              AND sessions.conversation_binding_id = ?
              AND sessions.status = 'completed'
              AND sessions.completed_message_id = ?
            """,
                (context.account_id, context.conversation_id, context.binding_id, message_id),
            )
        )
    if session is None:
        session = _row(
            conn.execute(
                """
            SELECT sessions.*, proposals.public_id AS proposal_public_id
            FROM openclaw_guided_edit_sessions AS sessions
            JOIN parser_outputs AS proposals ON proposals.id = sessions.current_parser_output_id
            WHERE sessions.channel_account_id = ?
              AND sessions.channel_conversation_id = ?
              AND sessions.conversation_binding_id = ?
              AND sessions.status = 'active'
            """,
                (context.account_id, context.conversation_id, context.binding_id),
            )
        )
    if session is None or not _context_matches(session, context):
        return None
    if (
        session["status"] == "active"
        and int(session["expires_at"]) > _now_epoch()
        and session["pending_message_id"] is None
    ):
        proposal, version, content_hash = _proposal_state(
            conn, int(session["current_parser_output_id"])
        )
        if str(proposal["parse_status"]) in TERMINAL_STATUSES:
            raise GuidedEditError("proposal_terminal")
        if version != int(session["current_proposal_version"]) or (
            content_hash != session["current_content_hash"]
        ):
            raise GuidedEditError("stale_session")
    return session


def get_active_session(
    conn: sqlite3.Connection, context: HumanActionContext
) -> dict[str, Any] | None:
    session = get_context_session(conn, context)
    if (
        session is None
        or session["status"] != "active"
        or (int(session["expires_at"]) <= _now_epoch())
    ):
        return None
    return session


def get_session_by_public_id(
    conn: sqlite3.Connection, session_public_id: str, context: HumanActionContext
) -> dict[str, Any] | None:
    session = _row(
        conn.execute(
            """
        SELECT sessions.*, proposals.public_id AS proposal_public_id
        FROM openclaw_guided_edit_sessions AS sessions
        JOIN parser_outputs AS proposals ON proposals.id = sessions.current_parser_output_id
        WHERE sessions.session_public_id = ?
        """,
            (session_public_id,),
        )
    )
    return session if session is not None and _context_matches(session, context) else None


def find_applied_replay(
    conn: sqlite3.Connection, session_id: int, message_id: int
) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            """
        SELECT events.*, proposals.public_id AS proposal_public_id
        FROM openclaw_guided_edit_events AS events
        JOIN parser_outputs AS proposals ON proposals.id = events.after_parser_output_id
        WHERE events.session_id = ? AND events.telegram_message_id = ?
          AND events.event_type = 'update_applied'
        """,
            (session_id, message_id),
        )
    )


def find_refused_replay(
    conn: sqlite3.Connection, session_id: int, message_id: int
) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            """
        SELECT * FROM openclaw_guided_edit_events
        WHERE session_id = ? AND telegram_message_id = ?
          AND event_type = 'update_refused'
        """,
            (session_id, message_id),
        )
    )


def request_update(
    conn: sqlite3.Connection,
    session: dict[str, Any],
    *,
    message_id: int,
    operation_key: str,
    field_name: str,
    field_value: str,
) -> dict[str, Any]:
    if field_name not in ALLOWED_FIELDS or not field_value:
        raise GuidedEditError("invalid_update")
    value_json = json.dumps(field_value, ensure_ascii=True, separators=(",", ":"))
    created_at = _now_text()
    _begin(conn)
    try:
        current = _row(
            conn.execute(
                "SELECT * FROM openclaw_guided_edit_sessions WHERE id = ?",
                (session["id"],),
            )
        )
        if current is None or current["status"] != "active":
            raise GuidedEditError("session_terminal")
        if int(current["expires_at"]) <= _now_epoch():
            raise GuidedEditError("session_expired")
        if current["pending_message_id"] is not None:
            raise GuidedEditError("pending_recovery")
        if message_id <= int(current["last_claimed_message_id"]):
            raise GuidedEditError("stale_message")
        duplicate = conn.execute(
            "SELECT 1 FROM openclaw_guided_edit_events "
            "WHERE session_id = ? AND telegram_message_id = ?",
            (current["id"], message_id),
        ).fetchone()
        if duplicate is not None:
            raise GuidedEditError("message_reused")
        before = (
            int(current["current_parser_output_id"]),
            int(current["current_proposal_version"]),
            str(current["current_content_hash"]),
        )
        conn.execute(
            """
            UPDATE openclaw_guided_edit_sessions
            SET pending_message_id = ?, pending_operation_key = ?,
                pending_field_name = ?, pending_field_value_json = ?,
                last_claimed_message_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                message_id,
                operation_key,
                field_name,
                value_json,
                message_id,
                created_at,
                current["id"],
            ),
        )
        _append_event(
            conn,
            current,
            "update_requested",
            message_id=message_id,
            operation_key=operation_key,
            field_name=field_name,
            field_value_json=value_json,
            before=before,
            created_at=created_at,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    refreshed = _row(
        conn.execute("SELECT * FROM openclaw_guided_edit_sessions WHERE id = ?", (session["id"],))
    )
    assert refreshed is not None
    return refreshed


def require_pending_authority_in_transaction(conn: sqlite3.Connection, session_id: int) -> None:
    """Recheck the session authorization after the core boundary owns the write lock."""
    if not conn.in_transaction:
        raise GuidedEditError("transaction_conflict")
    current = _row(
        conn.execute("SELECT * FROM openclaw_guided_edit_sessions WHERE id = ?", (session_id,))
    )
    if current is None or current["status"] != "active":
        raise GuidedEditError("session_terminal")
    if current["pending_message_id"] is None:
        raise GuidedEditError("pending_missing")
    if int(current["expires_at"]) <= _now_epoch():
        raise GuidedEditError("session_expired")
    proposal, version, content_hash = _proposal_state(
        conn, int(current["current_parser_output_id"])
    )
    if str(proposal["parse_status"]) in TERMINAL_STATUSES:
        raise GuidedEditError("proposal_terminal")
    if version != int(current["current_proposal_version"]) or (
        content_hash != current["current_content_hash"]
    ):
        raise GuidedEditError("stale_session")


def record_update_applied(
    conn: sqlite3.Connection,
    session: dict[str, Any],
    *,
    parser_output_id: int,
    proposal_version: int,
    content_hash: str,
) -> None:
    created_at = _now_text()
    _begin(conn)
    try:
        current = _row(
            conn.execute(
                "SELECT * FROM openclaw_guided_edit_sessions WHERE id = ?", (session["id"],)
            )
        )
        if current is None:
            raise GuidedEditError("pending_missing")
        if not _pending_identity_matches(current, session):
            if session["pending_message_id"] is None:
                raise GuidedEditError("pending_missing")
            replay = find_applied_replay(
                conn, int(current["id"]), int(session["pending_message_id"])
            )
            if (
                replay is not None
                and _event_matches_pending_snapshot(replay, session)
                and (
                    int(replay["after_parser_output_id"]) == parser_output_id
                    and int(replay["after_proposal_version"]) == proposal_version
                    and str(replay["after_content_hash"]) == content_hash
                )
            ):
                conn.commit()
                return
            raise GuidedEditError("pending_missing")
        persisted_state = pending_core_state(conn, current)
        expected_state = (parser_output_id, proposal_version, content_hash)
        if persisted_state != expected_state:
            raise GuidedEditError("pending_recovery")
        _settle_pending_in_transaction(
            conn,
            current,
            core_state=persisted_state,
            created_at=created_at,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def record_update_refused(
    conn: sqlite3.Connection, session: dict[str, Any], refusal_code: str
) -> None:
    created_at = _now_text()
    _begin(conn)
    try:
        current = _row(
            conn.execute(
                "SELECT * FROM openclaw_guided_edit_sessions WHERE id = ?", (session["id"],)
            )
        )
        if current is None:
            raise GuidedEditError("pending_missing")
        if not _pending_identity_matches(current, session):
            if session["pending_message_id"] is None:
                raise GuidedEditError("pending_missing")
            message_id = int(session["pending_message_id"])
            applied = find_applied_replay(conn, int(current["id"]), message_id)
            if applied is not None and _event_matches_pending_snapshot(applied, session):
                conn.commit()
                return
            replay = find_refused_replay(conn, int(current["id"]), message_id)
            if (
                replay is not None
                and _event_matches_pending_snapshot(replay, session)
                and replay["refusal_code"] == refusal_code
            ):
                conn.commit()
                return
            raise GuidedEditError("pending_conflict")
        core_state = pending_core_state(conn, current)
        _settle_pending_in_transaction(
            conn,
            current,
            core_state=core_state,
            refusal_code=refusal_code,
            created_at=created_at,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def complete_session(
    conn: sqlite3.Connection,
    session: dict[str, Any],
    *,
    context: HumanActionContext,
    message_id: int,
) -> dict[str, Any]:
    created_at = _now_text()
    _begin(conn)
    try:
        current = _row(
            conn.execute(
                "SELECT * FROM openclaw_guided_edit_sessions WHERE id = ?", (session["id"],)
            )
        )
        if current is None or not _context_matches(current, context):
            raise GuidedEditError("context_mismatch")
        if current["status"] == "completed" and current["completed_message_id"] == message_id:
            conn.commit()
            return current
        if current["status"] != "active" or current["pending_message_id"] is not None:
            raise GuidedEditError("session_not_completable")
        if int(current["expires_at"]) <= _now_epoch():
            raise GuidedEditError("session_expired")
        if message_id <= int(current["last_claimed_message_id"]):
            raise GuidedEditError("stale_message")
        proposal, version, content_hash = _proposal_state(
            conn, int(current["current_parser_output_id"])
        )
        if str(proposal["parse_status"]) in TERMINAL_STATUSES:
            raise GuidedEditError("proposal_terminal")
        if version != int(current["current_proposal_version"]) or (
            content_hash != current["current_content_hash"]
        ):
            raise GuidedEditError("stale_session")
        state = (
            int(current["current_parser_output_id"]),
            int(current["current_proposal_version"]),
            str(current["current_content_hash"]),
        )
        _append_event(
            conn,
            current,
            "completed",
            message_id=message_id,
            before=state,
            after=state,
            created_at=created_at,
        )
        conn.execute(
            "UPDATE openclaw_guided_edit_sessions "
            "SET status = 'completed', completed_message_id = ?, "
            "last_claimed_message_id = ?, updated_at = ? WHERE id = ?",
            (message_id, message_id, created_at, current["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    completed = _row(
        conn.execute(
            """
        SELECT sessions.*, proposals.public_id AS proposal_public_id
        FROM openclaw_guided_edit_sessions AS sessions
        JOIN parser_outputs AS proposals ON proposals.id = sessions.current_parser_output_id
        WHERE sessions.id = ?
        """,
            (session["id"],),
        )
    )
    assert completed is not None
    return completed


def claim_review_batch(
    conn: sqlite3.Connection,
    session: dict[str, Any],
    *,
    context: HumanActionContext,
    message_id: int,
) -> str:
    """Claim one durable, currently usable review-reference generation."""
    created_at = _now_text()
    _begin(conn)
    try:
        now = _now_epoch()
        current = _row(
            conn.execute(
                "SELECT * FROM openclaw_guided_edit_sessions WHERE id = ?", (session["id"],)
            )
        )
        if current is None or not _context_matches(current, context):
            raise GuidedEditError("context_mismatch")
        if current["status"] != "completed" or current["completed_message_id"] != message_id:
            raise GuidedEditError("session_terminal")
        proposal, version, content_hash = _proposal_state(
            conn, int(current["current_parser_output_id"])
        )
        if str(proposal["parse_status"]) in TERMINAL_STATUSES:
            raise GuidedEditError("proposal_terminal")
        if version != int(current["current_proposal_version"]) or (
            content_hash != current["current_content_hash"]
        ):
            raise GuidedEditError("stale_session")

        latest = _row(
            conn.execute(
                """
            SELECT generations.*, COUNT(refs.id) AS reference_count,
                   MIN(refs.expires_at) AS min_reference_expiry,
                   COUNT(redemptions.id) AS redemption_count
            FROM openclaw_guided_edit_review_generations AS generations
            LEFT JOIN openclaw_human_action_references AS refs
              ON refs.issuance_idempotency_key =
                 'bridge-human-action-issue:' || generations.reference_batch_id
            LEFT JOIN openclaw_human_action_redemptions AS redemptions
              ON redemptions.reference_id = refs.id
            WHERE generations.session_id = ?
            GROUP BY generations.id
            ORDER BY generations.generation DESC
            LIMIT 1
            """,
                (current["id"],),
            )
        )
        if latest is not None and (
            int(latest["reference_count"]) == 0
            or (
                int(latest["redemption_count"]) == 0
                and int(latest["min_reference_expiry"])
                > now + REVIEW_REFERENCE_MIN_REMAINING_SECONDS
            )
        ):
            batch_id = str(latest["reference_batch_id"])
            conn.commit()
            return batch_id

        generation = 1 if latest is None else int(latest["generation"]) + 1
        batch_id = _review_batch_id(str(current["session_public_id"]), message_id, generation)
        conn.execute(
            """
            INSERT INTO openclaw_guided_edit_review_generations (
                session_id, generation, reference_batch_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (current["id"], generation, batch_id, created_at),
        )
        conn.commit()
        return batch_id
    except Exception:
        conn.rollback()
        raise


__all__ = [
    "ALLOWED_FIELDS",
    "GuidedEditError",
    "active_d1_card_for_session",
    "begin_session_in_transaction",
    "claim_review_batch",
    "complete_session",
    "d1_compatibility_operation_public_id",
    "find_applied_replay",
    "find_refused_replay",
    "get_active_session",
    "get_context_session",
    "get_session_by_public_id",
    "pending_core_state",
    "record_update_applied",
    "record_update_refused",
    "require_pending_authority_in_transaction",
    "request_update",
    "session_for_reference",
]
