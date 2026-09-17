"""Parser confirmation commands backed by the authoritative unit of work."""

from __future__ import annotations

import sqlite3
from typing import Any

from finance_core.parser_proposals.human_drafts import HumanDraftDecisionBinding
from finance_core.parser_proposals.service import (
    ParserConfirmationError,
    confirm_parser_proposal,
)

CLI_ACTOR_TYPE = "human"
SUPPORTED_ACTOR_TYPES = frozenset({"human", "user"})


def confirm_proposal(
    conn: sqlite3.Connection,
    parser_output_id: int,
    *,
    actor: str,
    actor_type: str = CLI_ACTOR_TYPE,
    reason: str | None = None,
    confirmation_public_id: str | None = None,
    confirmation_channel: str = "cli",
    expected_content_hash: str | None = None,
    expected_version: int | None = None,
    d1_decision_binding: HumanDraftDecisionBinding | None = None,
) -> dict[str, Any]:
    """Confirm through the authoritative service-owned transaction.

    ``actor`` is the authenticated human identity supplied by the caller's
    authentication boundary.  The compatibility ``user`` actor type is
    normalized to the persisted ``human`` type; non-human types are rejected.
    ``expected_content_hash``/``expected_version`` enable the service-owned
    atomic stale-state guard inside the decision transaction.
    """
    return confirm_parser_proposal(
        conn,
        parser_output_id,
        authenticated_actor_id=actor,
        actor_type=actor_type,
        decision="confirmed",
        confirmation_channel=confirmation_channel,
        reason=reason,
        confirmation_public_id=confirmation_public_id,
        expected_content_hash=expected_content_hash,
        expected_version=expected_version,
        d1_decision_binding=d1_decision_binding,
    )


def reject_proposal(
    conn: sqlite3.Connection,
    parser_output_id: int,
    *,
    actor: str,
    actor_type: str = CLI_ACTOR_TYPE,
    reason: str | None = None,
    confirmation_public_id: str | None = None,
    confirmation_channel: str = "cli",
    expected_content_hash: str | None = None,
    expected_version: int | None = None,
    d1_decision_binding: HumanDraftDecisionBinding | None = None,
) -> dict[str, Any]:
    """Reject through the same authoritative, atomic lifecycle boundary."""
    return confirm_parser_proposal(
        conn,
        parser_output_id,
        authenticated_actor_id=actor,
        actor_type=actor_type,
        decision="rejected",
        confirmation_channel=confirmation_channel,
        reason=reason,
        confirmation_public_id=confirmation_public_id,
        expected_content_hash=expected_content_hash,
        expected_version=expected_version,
        d1_decision_binding=d1_decision_binding,
    )


def get_proposal_history(conn: sqlite3.Connection, parser_output_id: int) -> list[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT id, parser_output_id, from_status, to_status, event_type,
               event_reason, actor_type, actor_identifier, event_payload, created_at
        FROM parser_proposal_events
        WHERE parser_output_id = ?
        ORDER BY created_at ASC, id ASC
        """,
        (parser_output_id,),
    )
    rows = cursor.fetchall()
    return [
        dict(row)
        if isinstance(row, sqlite3.Row)
        else dict(zip((column[0] for column in cursor.description), row, strict=True))
        for row in rows
    ]


__all__ = [
    "CLI_ACTOR_TYPE",
    "ParserConfirmationError",
    "SUPPORTED_ACTOR_TYPES",
    "confirm_proposal",
    "get_proposal_history",
    "reject_proposal",
]
