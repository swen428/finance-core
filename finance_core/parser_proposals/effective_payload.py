"""Single authoritative effective proposal payload resolver.

Every service-level operation that needs to know which payload to hash,
confirm, convert, complete, or present must use this module.  There is
exactly one definition of ::

    original immutable parser payload
    + latest append-only authenticated completion version
    = effective proposal payload

Callers must hold or pass a transaction-neutral ``sqlite3.Connection``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


class EffectivePayloadError(ValueError):
    """Raised when the effective payload cannot be resolved safely."""


def resolve_effective_payload(
    conn: sqlite3.Connection,
    proposal: dict[str, Any],
) -> tuple[dict[str, Any], str | None, int]:
    """Return ``(effective_payload, latest_completion_public_id, version_number)``.

    * When no completion record exists the effective payload is the original
      immutable ``parsed_payload`` and the public id / version are ``None`` / 0.
    * When a completion record exists the effective payload is the cumulative
      completed payload from the latest successful version.

    The caller owns the returned dict — it is a fresh ``json.loads`` result,
    not a reference into the proposal row.
    """
    row = conn.execute(
        """
        SELECT completion_public_id, completed_payload_json, version_number
        FROM parser_proposal_completions
        WHERE parser_output_id = ?
        ORDER BY version_number DESC
        LIMIT 1
        """,
        (proposal["id"],),
    ).fetchone()

    if row is None:
        try:
            payload = json.loads(proposal["parsed_payload"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise EffectivePayloadError("Parser proposal JSON is invalid") from exc
        if not isinstance(payload, dict):
            raise EffectivePayloadError("Parser proposal JSON must be an object")
        return payload, None, 0

    if isinstance(row, sqlite3.Row):
        completed = json.loads(row["completed_payload_json"])
        cid: str | None = row["completion_public_id"]
        version: int = row["version_number"]
    else:
        completed = json.loads(row[1])
        cid = row[0]
        version = int(row[2])

    if not isinstance(completed, dict):
        raise EffectivePayloadError("Completed payload JSON must be an object")
    return completed, cid, version


__all__ = [
    "EffectivePayloadError",
    "resolve_effective_payload",
]
